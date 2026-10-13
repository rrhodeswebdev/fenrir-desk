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
        private static readonly TimeSpan FlattenQuiet = TimeSpan.FromSeconds(5);

        private readonly FenrirConfig config;
        private readonly string seenPath;
        private readonly BlockingCollection<Action> work = new BlockingCollection<Action>();
        private Timer sweep;

        // worker thread only
        private readonly Dictionary<string, DateTime> seen = new Dictionary<string, DateTime>(); // client id -> first seen, UTC
        private readonly Dictionary<string, Working> brackets = new Dictionary<string, Working>();
        private readonly HashSet<Account> attached = new HashSet<Account>();
        private readonly Dictionary<string, DateTime> lastFlatten = new Dictionary<string, DateTime>();

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
            public int Protected; // entry quantity the stop and target cover
            public DateTime SubmittedAt;
            public bool CancelSent;
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
                    FenrirLog.Info("broker: " + e);
                }
            }
        }

        public void Post(Action action)
        {
            try
            {
                work.Add(action);
            }
            catch (InvalidOperationException)
            {
                // stopped
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
            return Call(() => PlaceBracket(body));
        }

        private Dictionary<string, object> PlaceBracket(Dictionary<string, object> body)
        {
            string cid;
            Account account;
            Dictionary<string, object> refused = Check(body, true, out cid, out account);
            if (refused != null) return refused;
            Instrument instrument = FindInstrument(Str(body, "instrument"));
            if (instrument == null) return Ack(false, false, "unknown instrument");
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

        public Dictionary<string, object> Flatten(Dictionary<string, object> body)
        {
            return Call(() =>
            {
                string cid;
                Account account;
                Dictionary<string, object> refused = Check(body, true, out cid, out account);
                if (refused != null) return refused;
                Claim(cid);
                FlattenAccount(account, false);
                return Ack(true, false, null);
            });
        }

        public Dictionary<string, object> Cancel(Dictionary<string, object> body)
        {
            return Call(() =>
            {
                string cid;
                Account account;
                Dictionary<string, object> refused = Check(body, true, out cid, out account);
                if (refused != null) return refused;
                string orderId = Str(body, "order_id");
                Order order = Snapshot(account.Orders)
                    .FirstOrDefault(o => o.OrderId == orderId && !Order.IsTerminalState(o.OrderState));
                if (string.IsNullOrEmpty(orderId) || order == null) return Ack(false, false, "unknown order");
                Claim(cid);
                account.Cancel(new[] { order });
                return Ack(true, false, null);
            });
        }

        public Dictionary<string, object> FlattenAll(Dictionary<string, object> body)
        {
            return Call(() =>
            {
                string cid;
                Account unused;
                Dictionary<string, object> refused = Check(body, false, out cid, out unused);
                if (refused != null) return refused;
                Claim(cid);
                foreach (Account a in Managed()) FlattenAccount(a, true);
                return Ack(true, false, null);
            });
        }

        // Closes whatever position is open now (nothing when flat) and cancels Fenrir's working orders
        // (entry, stop, target), or with everything every working order, on those instruments.
        // NinjaTrader's Flatten works per instrument and also cancels other working orders there.
        private bool FlattenAccount(Account a, bool everything)
        {
            Dictionary<string, Instrument> instruments = new Dictionary<string, Instrument>();
            foreach (Position p in Snapshot(a.Positions))
                if (p.MarketPosition != MarketPosition.Flat) instruments[p.Instrument.FullName] = p.Instrument;
            foreach (Order o in Snapshot(a.Orders))
                if (!Order.IsTerminalState(o.OrderState) && (everything || ClientId(o.Name) != null))
                    instruments[o.Instrument.FullName] = o.Instrument;
            return instruments.Count > 0 && FlattenInstruments(a, instruments.Values);
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

        // every 500 ms: new accounts, and entries still incomplete 2 s after they were sent
        private void Sweep()
        {
            AttachManaged();
            DateTime now = DateTime.UtcNow;
            foreach (Working b in brackets.Values)
            {
                if (b.CancelSent || now - b.SubmittedAt < EntryTimeout || Order.IsTerminalState(b.Entry.OrderState)) continue;
                b.CancelSent = true; // fills that still arrive are protected by OnFenrirOrder
                FenrirLog.Info("entry " + b.ClientId + " not complete after 2 s: cancelling the remainder");
                b.Account.Cancel(new[] { b.Entry });
            }
        }

        // NinjaTrader's thread: never throw, never wait
        private void OnOrderUpdate(object sender, OrderEventArgs e)
        {
            try
            {
                Order o = e.Order;
                if (ClientId(o.Name) == null) return;
                OrderState state = e.OrderState;
                int filled = e.Filled;
                Post(() => OnFenrirOrder(o, state, filled));
            }
            catch (Exception ex)
            {
                FenrirLog.Info("order update: " + ex.Message);
            }
        }

        private void OnFenrirOrder(Order o, OrderState state, int filled)
        {
            Working b;
            // only brackets placed since this start: NinjaTrader may replay older orders' updates
            if (!brackets.TryGetValue(ClientId(o.Name), out b)) return;
            string role = Role(o.Name);
            if (role == "entry")
            {
                if (filled > b.Protected) Protect(b, filled);
            }
            else if (state == OrderState.Rejected)
            {
                // a position is never left without its stop
                FenrirLog.Alert(role + " rejected for " + b.ClientId + ": flattening " + b.Instrument.FullName + " on " + b.Account.Name);
                FlattenInstruments(b.Account, new[] { b.Instrument });
            }
        }

        // On every entry fill the OCO stop and target cover the total filled so far (DEC-5):
        // placed on the first fill, resized on later ones.
        private void Protect(Working b, int filled)
        {
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
                else if (!Order.IsTerminalState(b.StopOrder.OrderState) && !Order.IsTerminalState(b.TargetOrder.OrderState))
                {
                    // Account.Change reads QuantityChanged; setting Quantity does nothing
                    b.StopOrder.QuantityChanged = filled;
                    b.TargetOrder.QuantityChanged = filled;
                    b.Account.Change(new[] { b.StopOrder, b.TargetOrder });
                }
                else
                {
                    FenrirLog.Alert("entry " + b.ClientId + " filled more after its stop or target closed: flattening " + b.Instrument.FullName);
                    FlattenInstruments(b.Account, new[] { b.Instrument });
                }
                b.Protected = filled;
            }
            catch (Exception e)
            {
                FenrirLog.Alert("could not protect " + b.ClientId + " (" + e.Message + "): flattening " + b.Instrument.FullName);
                FlattenInstruments(b.Account, new[] { b.Instrument });
            }
        }

        // NinjaTrader's Flatten cancels the instrument's working orders and closes its position. Sent
        // again while that close is still in flight it might close twice, so Fenrir flattens one
        // account and instrument at most once per 5 s.
        private bool FlattenInstruments(Account a, IEnumerable<Instrument> instruments)
        {
            DateTime now = DateTime.UtcNow;
            List<Instrument> due = new List<Instrument>();
            foreach (Instrument i in instruments)
            {
                string key = a.Name + "|" + i.FullName;
                DateTime last;
                if (lastFlatten.TryGetValue(key, out last) && now - last < FlattenQuiet) continue;
                lastFlatten[key] = now;
                due.Add(i);
            }
            if (due.Count == 0) return false;
            a.Flatten(due);
            return true;
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
