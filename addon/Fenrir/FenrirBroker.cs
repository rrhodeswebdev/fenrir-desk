#region Using declarations
using System;
using System.Collections.Generic;
using System.Linq;
using NinjaTrader.Cbi;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
    // Forwards to NinjaTrader's Account API for the managed accounts only (DEC-5) and turns what
    // it returns into JSON-ready dictionaries.
    public class FenrirBroker
    {
        private readonly FenrirConfig config;

        public FenrirBroker(FenrirConfig config)
        {
            this.config = config;
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
