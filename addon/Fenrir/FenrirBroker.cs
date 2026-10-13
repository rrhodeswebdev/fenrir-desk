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
