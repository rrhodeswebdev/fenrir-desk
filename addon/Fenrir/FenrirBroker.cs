#region Using declarations
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Threading;
using System.Web.Script.Serialization;
using NinjaTrader.Cbi;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
    // Forwards to NinjaTrader's Account API for the managed accounts only (DEC-5) and turns what
    // it returns into JSON-ready dictionaries.
    //
    // Threads: reads (accounts, positions, orders) run on the caller's thread. Everything that
    // places, changes or cancels an order, and all of Fenrir's own state (seen client ids,
    // brackets), runs on one worker thread, in order. NinjaTrader's event handlers only queue work
    // for it, so they never wait on Fenrir, and Fenrir never holds a lock while calling NinjaTrader.
    public class FenrirBroker
    {
        private static readonly TimeSpan EntryTimeout = TimeSpan.FromSeconds(2);
        private static readonly TimeSpan SeenFor = TimeSpan.FromDays(7);
        private static readonly TimeSpan CloseGrace = TimeSpan.FromSeconds(2);
        private static readonly TimeSpan StaleBracket = TimeSpan.FromSeconds(4); // under the client's 5 s timeout
        private static readonly TimeSpan ResizeSettle = TimeSpan.FromSeconds(3);

        private readonly FenrirConfig config;
        private readonly string seenPath;
        private readonly BlockingCollection<Action> work = new BlockingCollection<Action>();
        private Timer sweep;

        // worker thread only
        private readonly Dictionary<string, DateTime> seen = new Dictionary<string, DateTime>(); // client id -> first seen, UTC
        private readonly Dictionary<string, Working> brackets = new Dictionary<string, Working>();
        private readonly HashSet<Account> attached = new HashSet<Account>();
        private readonly Dictionary<string, FlatRequest> mustBeFlat = new Dictionary<string, FlatRequest>(); // "account|instrument"

        // a bracket placed since this start
        private class Working
        {
            public string ClientId;
            public Account Account;
            public Instrument Instrument;
            public bool Long;
            public double Stop;
            public double Target;
            public Order Entry;
            public Order StopOrder;
            public Order TargetOrder;
            public int Protected; // entry quantity handled: covered by the stop and target, or closed
            public int Closed; // of that, closed at market by CloseExactly
            public DateTime SubmittedAt;
            public DateTime ResizedAt;
            public bool CancelSent;
            public bool Closing; // its instrument is being flattened: later entry fills are closed, not protected
        }

        // an account and instrument Fenrir keeps flattening until NinjaTrader shows it flat
        private class FlatRequest
        {
            public Account Account;
            public Instrument Instrument;
            public bool Everything; // also every non-Fenrir working order there
            public DateTime GraceUntil; // just flattened: its close may not be visible yet
            public int Tries;
        }

        public FenrirBroker(FenrirConfig config)
        {
            this.config = config;
            seenPath = Path.Combine(FenrirConfig.Dir, "fenrir-addon.seen.json");
        }

        public void Start()
        {
            Thread worker = new Thread(WorkLoop);
            worker.IsBackground = true;
            worker.Name = "Fenrir broker";
            worker.Start();
            Post(LoadSeen);
            Post(AttachManaged);
            sweep = new Timer(_ => Post(Sweep), null, 500, 500);
        }

        public void Stop()
        {
            if (sweep != null) sweep.Dispose();
            Post(DetachAll);
            work.CompleteAdding();
        }

        private void WorkLoop()
        {
            foreach (Action action in work.GetConsumingEnumerable())
            {
                try
                {
                    action();
                }
                catch (Exception e)
                {
                    FenrirLog.Alert("broker: " + e);
                }
            }
        }

        public void Post(Action action)
        {
            try
            {
                work.Add(action);
            }
            catch (Exception)
            {
                // stopped; also called from timers and NinjaTrader's threads, so it never throws
            }
        }

        // runs f on the worker and waits for its result
        private T Call<T>(Func<T> f)
        {
            T result = default(T);
            Exception error = null;
            ManualResetEventSlim done = new ManualResetEventSlim();
            Post(() =>
            {
                try
                {
                    result = f();
                }
                catch (Exception e)
                {
                    error = e;
                }
                finally
                {
                    done.Set();
                }
            });
            if (!done.Wait(TimeSpan.FromSeconds(10)))
                throw new TimeoutException("NinjaTrader did not answer within 10 s; the order may still land");
            if (error != null) throw error;
            return result;
        }

        // Account.All is locked while enumerating, as NinjaTrader's help guide does
        public List<Account> Managed()
        {
            lock (Account.All)
                return Account.All.Where(a => config.ManagedAccounts.Contains(a.Name)).ToList();
        }

        public Account Find(string id)
        {
            return Managed().FirstOrDefault(a => a.Name == id);
        }

        public List<object> Accounts()
        {
            List<object> rows = new List<object>();
            foreach (Account a in Managed())
            {
                Dictionary<string, object> row = AccountJson(a);
                if (row != null) rows.Add(row);
            }
            return rows;
        }

        public List<object> Positions(string accountId)
        {
            Account a = Find(accountId);
            if (a == null) return new List<object>();
            return Snapshot(a.Positions).Where(p => p.MarketPosition != MarketPosition.Flat)
                .Select(p => (object)PositionJson(p)).ToList();
        }

        // working orders only: not in a terminal state, with something left to fill
        public List<object> Orders(string accountId)
        {
            Account a = Find(accountId);
            if (a == null) return new List<object>();
            return Snapshot(a.Orders).Where(o => !Order.IsTerminalState(o.OrderState) && o.Quantity - o.Filled > 0)
                .Select(o => (object)OrderJson(o)).ToList();
        }

        public Dictionary<string, object> Bracket(Dictionary<string, object> body)
        {
            DateTime received = DateTime.UtcNow;
            return Call(() => PlaceBracket(body, received));
        }

        private Dictionary<string, object> PlaceBracket(Dictionary<string, object> body, DateTime received)
        {
            string cid;
            Account account;
            Dictionary<string, object> refused = Check(body, true, out cid, out account);
            if (refused != null) return refused;
            // the client has given up by now; a late market entry would trade at another price
            if (DateTime.UtcNow - received > StaleBracket)
                return Ack(false, false, "not placed: NinjaTrader was busy for over 4 s");
            Instrument instrument = FindInstrument(Str(body, "instrument"));
            if (instrument == null) return Ack(false, false, "unknown instrument");
            if (mustBeFlat.ContainsKey(Key(account, instrument)))
                return Ack(false, false, "not placed: " + instrument.FullName + " is being flattened on " + account.Name);
            string side = Str(body, "side");
            int qty = Int(body, "qty");
            double stop = Num(body, "stop");
            double target = Num(body, "target");
            if ((side != "long" && side != "short") || qty <= 0 || double.IsNaN(stop) || double.IsNaN(target))
                return Ack(false, false, "invalid bracket: side, qty, stop or target");
            bool isLong = side == "long";
            if (isLong ? stop >= target : stop <= target)
                return Ack(false, false, "invalid bracket: stop must be on the losing side of target");

            Claim(cid);
            try
            {
                Order entry = account.CreateOrder(instrument, isLong ? OrderAction.Buy : OrderAction.SellShort,
                    OrderType.Market, OrderEntry.Automated, TimeInForce.Day, qty, 0, 0, string.Empty,
                    OrderName(cid, 'E'), NinjaTrader.Core.Globals.MaxDate, null);
                brackets[cid] = new Working
                {
                    ClientId = cid, Account = account, Instrument = instrument, Long = isLong,
                    Stop = stop, Target = target, Entry = entry, SubmittedAt = DateTime.UtcNow,
                };
                account.Submit(new[] { entry });
            }
            catch (Exception e)
            {
                // nothing was placed: a retry must not come back duplicate
                brackets.Remove(cid);
                Release(cid);
                FenrirLog.Alert("bracket " + cid + " not placed: " + e.Message);
                return Ack(false, false, "submit failed: " + e.Message);
            }
            return Ack(true, false, null);
        }

        // Checks every POST shares. An unmanaged account is refused before the id is recorded, so a
        // retry after fixing managed_accounts is not a false duplicate.
        private Dictionary<string, object> Check(Dictionary<string, object> body, bool named, out string cid, out Account account)
        {
            cid = Str(body, "client_id");
            account = null;
            if (string.IsNullOrEmpty(cid)) return Ack(false, false, "client_id required");
            if (cid.Length > 24) return Ack(false, false, "client_id over 24 characters");
            if (named)
            {
                string id = Str(body, "account");
                if (id == null || !config.ManagedAccounts.Contains(id)) return Ack(false, false, "unmanaged_account");
                if (seen.ContainsKey(cid)) return Ack(false, true, null);
                account = Find(id);
                if (account == null) return Ack(false, false, "unknown account");
                return null;
            }
            return seen.ContainsKey(cid) ? Ack(false, true, null) : null;
        }

        private static Instrument FindInstrument(string name)
        {
            if (string.IsNullOrEmpty(name)) return null;
            try
            {
                return Instrument.GetInstrument(name);
            }
            catch (Exception)
            {
                return null;
            }
        }

        private void Claim(string cid)
        {
            seen[cid] = DateTime.UtcNow;
            SaveSeen();
        }

        private void Release(string cid)
        {
            seen.Remove(cid);
            SaveSeen();
        }

        // fenrir-addon.seen.json beside the config: {"<client id>": "<first seen, UTC ISO 8601>"}, 7 days
        private void LoadSeen()
        {
            try
            {
                if (!File.Exists(seenPath)) return;
                Dictionary<string, object> raw =
                    new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(seenPath));
                DateTime cutoff = DateTime.UtcNow - SeenFor;
                foreach (KeyValuePair<string, object> kv in raw)
                {
                    DateTime at;
                    if (kv.Value is string && DateTime.TryParse((string)kv.Value, CultureInfo.InvariantCulture,
                            DateTimeStyles.RoundtripKind, out at) && at.ToUniversalTime() > cutoff)
                        seen[kv.Key] = at.ToUniversalTime();
                }
                FenrirLog.Info(string.Format("{0} client ids seen in the last 7 days", seen.Count));
            }
            catch (Exception e)
            {
                FenrirLog.Alert("could not read " + seenPath + " (" + e.Message + "): a retried order may be placed twice");
            }
        }

        private void SaveSeen()
        {
            try
            {
                DateTime cutoff = DateTime.UtcNow - SeenFor;
                foreach (string old in seen.Where(kv => kv.Value <= cutoff).Select(kv => kv.Key).ToList()) seen.Remove(old);
                Dictionary<string, object> raw = seen.ToDictionary(kv => kv.Key,
                    kv => (object)kv.Value.ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture));
                string tmp = seenPath + ".tmp";
                File.WriteAllText(tmp, new JavaScriptSerializer().Serialize(raw));
                if (File.Exists(seenPath)) File.Replace(tmp, seenPath, null);
                else File.Move(tmp, seenPath);
            }
            catch (Exception e)
            {
                FenrirLog.Alert("could not write " + seenPath + " (" + e.Message + "): dedupe will not survive a restart");
            }
        }

        // accounts whose connection comes up after start are picked up here too
        private void AttachManaged()
        {
            foreach (Account a in Managed())
                if (attached.Add(a)) a.OrderUpdate += OnOrderUpdate;
        }

        private void DetachAll()
        {
            foreach (Account a in attached) a.OrderUpdate -= OnOrderUpdate;
            attached.Clear();
        }

        // every 500 ms: new accounts, entries still incomplete 2 s after they were sent, stops that did
        // not take a resize, and every account and instrument that must become flat
        private void Sweep()
        {
            AttachManaged();
            DateTime now = DateTime.UtcNow;
            foreach (Working b in brackets.Values)
            {
                try
                {
                    if (!b.CancelSent && !b.Closing && now - b.SubmittedAt >= EntryTimeout && !Order.IsTerminalState(b.Entry.OrderState))
                    {
                        FenrirLog.Info("entry " + b.ClientId + " not complete after 2 s: cancelling the remainder");
                        b.Account.Cancel(new[] { b.Entry });
                        b.CancelSent = true; // fills that still arrive are protected by OnFenrirOrder
                    }
                    CheckStopSize(b, now);
                }
                catch (Exception e)
                {
                    FenrirLog.Alert("bracket " + b.ClientId + ": " + e.Message);
                }
            }
            foreach (KeyValuePair<string, FlatRequest> kv in mustBeFlat.ToList())
            {
                try
                {
                    if (Advance(kv.Value))
                    {
                        mustBeFlat.Remove(kv.Key);
                        FenrirLog.Info(kv.Value.Instrument.FullName + " flat on " + kv.Value.Account.Name);
                    }
                }
                catch (Exception e)
                {
                    FenrirLog.Alert("flattening " + kv.Key + ": " + e.Message + "; retrying");
                }
            }
        }

        // A resize NinjaTrader refused leaves the stop smaller than the position it guards: close the
        // difference. Never more than the position actually held, so exits that partly filled (or
        // an OCO NinjaTrader shrank after its target partly filled) are never closed twice.
        private void CheckStopSize(Working b, DateTime now)
        {
            if (b.Closing || b.StopOrder == null || now - b.ResizedAt < ResizeSettle) return;
            OrderState st = b.StopOrder.OrderState;
            if (st != OrderState.Accepted && st != OrderState.Working && st != OrderState.TriggerPending) return;
            int held = Held(b.Account, b.Instrument);
            if (b.Long ? held <= 0 : held >= 0) return;
            int expected = b.Protected - b.Closed - b.StopOrder.Filled - b.TargetOrder.Filled;
            int covered = b.StopOrder.Quantity - b.StopOrder.Filled;
            int open = Math.Min(expected, Math.Abs(held)) - covered;
            if (open > 0) CloseExactly(b, open, "stop " + b.ClientId + " covers " + covered + " of " + (covered + open));
        }

        // NinjaTrader's thread: never throw, never wait
        private void OnOrderUpdate(object sender, OrderEventArgs e)
        {
            try
            {
                Order o = e.Order;
                OrderState state = e.OrderState;
                int filled = e.Filled;
                ErrorCode error = e.Error;
                if (ClientId(o.Name) != null) Post(() => OnFenrirOrder(o, state, filled, error));
                if (state == OrderState.Rejected) Post(() => OnRejected(o));
            }
            catch (Exception ex)
            {
                FenrirLog.Info("order update: " + ex.Message);
            }
        }

        // a rejected close: no close is coming, try again after 1 s per attempt so far (at most 30 s)
        private void OnRejected(Order o)
        {
            FlatRequest r;
            if (mustBeFlat.TryGetValue(Key(o.Account, o.Instrument), out r))
                r.GraceUntil = DateTime.UtcNow + TimeSpan.FromSeconds(Math.Min(Math.Max(r.Tries, 1), 30));
        }

        private void OnFenrirOrder(Order o, OrderState state, int filled, ErrorCode error)
        {
            Working b;
            // only brackets placed since this start: NinjaTrader may replay older orders' updates
            if (!brackets.TryGetValue(ClientId(o.Name), out b)) return;
            string role = Role(o.Name);
            if (role == "entry")
            {
                if (filled <= b.Protected) return;
                if (b.Closing || mustBeFlat.ContainsKey(Key(b.Account, b.Instrument)))
                {
                    // being flattened: never protect again, close exactly what filled
                    int more = filled - b.Protected;
                    b.Protected = filled;
                    CloseExactly(b, more, "entry " + b.ClientId + " filled " + more + " while flattening");
                }
                else Protect(b, filled);
                return;
            }
            if (error != ErrorCode.NoError)
                FenrirLog.Alert(role + " " + b.ClientId + ": " + error + " (" + state + ")");
            if (b.Closing) return;
            if (state == OrderState.Rejected)
            {
                // a position is never left without its stop
                FenrirLog.Alert(role + " rejected for " + b.ClientId + ": flattening " + b.Instrument.FullName + " on " + b.Account.Name);
                RequestFlat(b.Account, b.Instrument, false);
            }
            else if (role == "stop" && state == OrderState.Cancelled && !Filled(b.TargetOrder) && Held(b.Account, b.Instrument) != 0)
            {
                FenrirLog.Alert("stop cancelled for " + b.ClientId + " while the position is held: flattening " + b.Instrument.FullName + " on " + b.Account.Name);
                RequestFlat(b.Account, b.Instrument, false);
            }
        }

        // On every entry fill the OCO stop and target cover the total filled so far (DEC-5):
        // placed on the first fill, resized on later ones.
        private void Protect(Working b, int filled)
        {
            int open = filled - b.Protected;
            try
            {
                if (b.StopOrder == null)
                {
                    OrderAction exit = b.Long ? OrderAction.Sell : OrderAction.BuyToCover;
                    string oco = "F|" + b.ClientId;
                    b.StopOrder = b.Account.CreateOrder(b.Instrument, exit, OrderType.StopMarket, OrderEntry.Automated,
                        TimeInForce.Gtc, filled, 0, b.Stop, oco, OrderName(b.ClientId, 'S'), NinjaTrader.Core.Globals.MaxDate, null);
                    b.TargetOrder = b.Account.CreateOrder(b.Instrument, exit, OrderType.Limit, OrderEntry.Automated,
                        TimeInForce.Gtc, filled, b.Target, 0, oco, OrderName(b.ClientId, 'T'), NinjaTrader.Core.Globals.MaxDate, null);
                    b.Account.Submit(new[] { b.StopOrder, b.TargetOrder });
                }
                else if (Live(b.StopOrder) && Live(b.TargetOrder))
                {
                    // Account.Change reads QuantityChanged; setting Quantity does nothing
                    b.StopOrder.QuantityChanged = filled;
                    b.TargetOrder.QuantityChanged = filled;
                    b.Account.Change(new[] { b.StopOrder, b.TargetOrder });
                    b.ResizedAt = DateTime.UtcNow;
                }
                else
                {
                    b.Protected = filled;
                    CloseExactly(b, open, "entry " + b.ClientId + " filled " + open + " more after its stop or target closed");
                    return;
                }
                b.Protected = filled;
            }
            catch (Exception e)
            {
                b.Protected = filled;
                FenrirLog.Alert("could not protect " + b.ClientId + " (" + e.Message + ")");
                CloseExactly(b, open, "unprotected fill of " + b.ClientId);
            }
        }

        // A market exit for exactly qty: whatever else is working, it cannot close more than that.
        // If even that fails, the instrument is flattened.
        private void CloseExactly(Working b, int qty, string why)
        {
            if (qty <= 0) return;
            b.Closed += qty;
            FenrirLog.Alert(why + ": closing " + qty + " " + b.Instrument.FullName + " on " + b.Account.Name + " at market");
            try
            {
                Order exit = b.Account.CreateOrder(b.Instrument, b.Long ? OrderAction.Sell : OrderAction.BuyToCover,
                    OrderType.Market, OrderEntry.Automated, TimeInForce.Day, qty, 0, 0, string.Empty, "Fenrir close",
                    NinjaTrader.Core.Globals.MaxDate, null);
                b.Account.Submit(new[] { exit });
            }
            catch (Exception e)
            {
                FenrirLog.Alert("market exit failed (" + e.Message + "): flattening " + b.Instrument.FullName);
                RequestFlat(b.Account, b.Instrument, false);
            }
        }

        // Flattens the account's instrument now and keeps at it, every sweep, until NinjaTrader shows
        // it flat; brackets working there stop being protected and their later fills are closed.
        // True when this starts a new request. Throws if NinjaTrader refuses the first attempt (the
        // sweep still retries).
        private bool RequestFlat(Account a, Instrument i, bool everything)
        {
            foreach (Working b in brackets.Values)
                if (b.Account == a && b.Instrument.FullName == i.FullName) b.Closing = true;
            string key = Key(a, i);
            FlatRequest r;
            bool added = !mustBeFlat.TryGetValue(key, out r);
            if (added)
            {
                r = new FlatRequest { Account = a, Instrument = i };
                mustBeFlat[key] = r;
            }
            r.Everything |= everything;
            if (Advance(r) && added) mustBeFlat.Remove(key);
            return added;
        }

        // One step toward flat; true once there is no position and nothing left to cancel.
        // NinjaTrader's Flatten cancels the instrument's working orders and closes its position. It is
        // never sent while a close is already working there (a market order on the reducing side that
        // is not a Fenrir entry) or in the grace after the last one, so a close in flight is never
        // sent twice.
        private bool Advance(FlatRequest r)
        {
            int held = Held(r.Account, r.Instrument);
            List<Order> working = Snapshot(r.Account.Orders)
                .Where(o => o.Instrument.FullName == r.Instrument.FullName && !Order.IsTerminalState(o.OrderState)).ToList();
            bool closing = held != 0 && working.Any(o => o.OrderType == OrderType.Market && Role(o.Name) != "entry"
                && (held > 0) == (o.OrderAction == OrderAction.Sell || o.OrderAction == OrderAction.SellShort));
            bool leftovers = working.Any(o => Live(o) && (r.Everything || ClientId(o.Name) != null));
            if (held == 0 && !leftovers) return true;
            if (closing || DateTime.UtcNow < r.GraceUntil) return false;
            r.Tries++;
            if (r.Tries == 5 || r.Tries % 30 == 0)
                FenrirLog.Alert(r.Instrument.FullName + " on " + r.Account.Name + " still not flat after " + r.Tries + " flatten attempts");
            try
            {
                r.Account.Flatten(new[] { r.Instrument });
            }
            catch (Exception)
            {
                r.GraceUntil = DateTime.UtcNow + TimeSpan.FromSeconds(Math.Min(r.Tries, 30)); // no retry storm
                throw;
            }
            r.GraceUntil = DateTime.UtcNow + CloseGrace;
            return false;
        }

        // signed position on one instrument
        private static int Held(Account a, Instrument i)
        {
            Position p = Snapshot(a.Positions).FirstOrDefault(x => x.Instrument.FullName == i.FullName);
            if (p == null) return 0;
            return p.MarketPosition == MarketPosition.Long ? p.Quantity : p.MarketPosition == MarketPosition.Short ? -p.Quantity : 0;
        }

        // working and not already on its way out
        private static bool Live(Order o)
        {
            return !Order.IsTerminalState(o.OrderState) && o.OrderState != OrderState.CancelPending
                && o.OrderState != OrderState.CancelSubmitted;
        }

        private static bool Filled(Order o)
        {
            return o != null && (o.OrderState == OrderState.Filled || o.OrderState == OrderState.PartFilled);
        }

        private static string Key(Account a, Instrument i)
        {
            return a.Name + "|" + i.FullName;
        }

        private static Dictionary<string, object> Ack(bool accepted, bool duplicate, string error)
        {
            return new Dictionary<string, object> { { "accepted", accepted }, { "duplicate", duplicate }, { "error", error } };
        }

        private static string Str(Dictionary<string, object> body, string key)
        {
            object v;
            return body != null && body.TryGetValue(key, out v) ? v as string : null;
        }

        // a JSON whole number, else 0
        private static int Int(Dictionary<string, object> body, string key)
        {
            object v;
            return body != null && body.TryGetValue(key, out v) && v is int ? (int)v : 0;
        }

        // a finite JSON number, else NaN
        private static double Num(Dictionary<string, object> body, string key)
        {
            object v;
            if (body == null || !body.TryGetValue(key, out v) || v == null || v is string || v is bool) return double.NaN;
            if (!(v is int || v is long || v is decimal || v is double)) return double.NaN;
            double x = Convert.ToDouble(v, CultureInfo.InvariantCulture);
            return Finite(x) ? x : double.NaN;
        }

        public static string OrderName(string clientId, char role)
        {
            return "F|" + clientId + "|" + role;
        }

        // NinjaTrader changes these collections on its own threads: copy them under their lock
        public static List<T> Snapshot<T>(ICollection<T> items)
        {
            lock (items) return items.ToList();
        }

        // Fenrir's order names carry the client id: "F|<client_id>|E" (entry), "|S" (stop), "|T" (target)
        public static string ClientId(string orderName)
        {
            if (orderName == null || !orderName.StartsWith("F|") || orderName.Length < 5) return null;
            int last = orderName.Length - 2;
            if (orderName[last] != '|' || "EST".IndexOf(orderName[last + 1]) < 0) return null;
            return orderName.Substring(2, last - 2);
        }

        public static string Role(string orderName)
        {
            if (ClientId(orderName) == null) return "other";
            switch (orderName[orderName.Length - 1])
            {
                case 'E': return "entry";
                case 'S': return "stop";
                default: return "target";
            }
        }

        public static Dictionary<string, object> PositionJson(Position p)
        {
            int qty = p.MarketPosition == MarketPosition.Long ? p.Quantity
                    : p.MarketPosition == MarketPosition.Short ? -p.Quantity : 0;
            return new Dictionary<string, object>
            {
                { "account", p.Account.Name },
                { "instrument", p.Instrument.FullName },
                { "qty", qty },
                { "avg_price", qty == 0 ? 0.0 : p.AveragePrice },
            };
        }

        // qty is the remaining quantity, unsigned; the side is in action
        public static Dictionary<string, object> OrderJson(Order o)
        {
            bool buy = o.OrderAction == OrderAction.Buy || o.OrderAction == OrderAction.BuyToCover;
            bool stop = o.OrderType == OrderType.StopMarket || o.OrderType == OrderType.StopLimit;
            string clientId = ClientId(o.Name);
            return new Dictionary<string, object>
            {
                { "account", o.Account.Name },
                { "order_id", o.OrderId ?? "" },
                { "client_id", clientId ?? "" },
                { "instrument", o.Instrument.FullName },
                { "role", Role(o.Name) },
                { "action", buy ? "buy" : "sell" },
                { "qty", Math.Max(0, o.Quantity - o.Filled) },
                { "price", stop ? o.StopPrice : o.LimitPrice },
                { "state", o.OrderState.ToString() },
            };
        }

        // null while NinjaTrader reports a non-finite figure: the client rejects NaN
        public static Dictionary<string, object> AccountJson(Account a)
        {
            double balance = a.Get(AccountItem.CashValue, Currency.UsDollar);
            double realized = a.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar);
            double unrealized = a.Get(AccountItem.UnrealizedProfitLoss, Currency.UsDollar);
            if (!Finite(balance) || !Finite(realized) || !Finite(unrealized)) return null;
            return new Dictionary<string, object>
            {
                { "id", a.Name },
                { "name", a.DisplayName },
                { "is_sim", a.Provider == Provider.Simulator || a.Provider == Provider.Playback },
                { "balance", balance },
                { "realized_pnl_today", realized },
                { "unrealized_pnl", unrealized },
            };
        }

        public static bool Finite(double x)
        {
            return !double.IsNaN(x) && !double.IsInfinity(x);
        }
    }
}
