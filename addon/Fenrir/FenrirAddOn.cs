#region Using declarations
using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Web.Script.Serialization;
using NinjaTrader.Cbi;
#endregion

// Fenrir Desk bridge (docs/contracts.md, "Plan 19/20"): accounts, positions and orders over
// HTTP + WebSocket for the Python app. No market data and no trading decisions.
namespace NinjaTrader.NinjaScript.AddOns
{
    // Documents\NinjaTrader 8\fenrir-addon.json
    public class FenrirConfig
    {
        public int Port = 8123;
        public string Token = "";
        public HashSet<string> ManagedAccounts = new HashSet<string>();

        public static string Dir
        {
            get { return NinjaTrader.Core.Globals.UserDataDir; }
        }

        public static FenrirConfig Load()
        {
            FenrirConfig config = new FenrirConfig();
            string path = Path.Combine(Dir, "fenrir-addon.json");
            if (!File.Exists(path))
            {
                FenrirLog.Alert("no " + path + "; copy fenrir-addon.example.json there and set the token");
                return config;
            }
            Dictionary<string, object> raw =
                new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(path));
            object v;
            if (raw.TryGetValue("port", out v) && v is int) config.Port = (int)v;
            if (raw.TryGetValue("token", out v) && v is string) config.Token = (string)v;
            if (raw.TryGetValue("managed_accounts", out v) && v is IEnumerable && !(v is string))
                foreach (object name in (IEnumerable)v)
                    if (name is string) config.ManagedAccounts.Add((string)name);
            return config;
        }
    }

    public static class FenrirLog
    {
        // NinjaScript Output window, tab 1
        public static void Info(string message)
        {
            NinjaTrader.Code.Output.Process("Fenrir: " + message, PrintTo.OutputTab1);
        }

        // also the Control Center Log tab, as an alert
        public static void Alert(string message)
        {
            Info(message);
            try
            {
                NinjaTrader.NinjaScript.NinjaScript.Log("Fenrir: " + message, LogLevel.Alert);
            }
            catch
            {
                // the Output window line above is enough
            }
        }
    }

    public class FenrirAddOn : AddOnBase
    {
        private bool started;
        private FenrirConfig config;
        private FenrirBroker broker;
        private FenrirServer server;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "Fenrir";
                Description = "Fenrir Desk bridge: accounts, positions and orders over HTTP + WebSocket. No market data, no decisions.";
            }
            else if (State == State.Active)
            {
                Start();
            }
            else if (State == State.Terminated)
            {
                Shutdown();
            }
        }

        private void Start()
        {
            if (started) return; // Active may be raised more than once
            started = true;
            try
            {
                config = FenrirConfig.Load();
                if (config.ManagedAccounts.Count == 0)
                    FenrirLog.Alert("managed_accounts is empty in fenrir-addon.json: Fenrir will not see or touch any account");
                broker = new FenrirBroker(config);
                if (config.Token.Length < 16)
                {
                    FenrirLog.Alert("set a token of at least 16 characters in fenrir-addon.json; server not started");
                    return;
                }
                server = new FenrirServer(config, broker);
                server.Start();
                FenrirLog.Info(string.Format("listening on port {0}", config.Port));
            }
            catch (Exception e)
            {
                FenrirLog.Alert("failed to start: " + e);
            }
        }

        private void Shutdown()
        {
            if (!started) return; // instances made only to list add-ons never started
            try
            {
                if (server != null) server.Stop();
            }
            catch (Exception e)
            {
                FenrirLog.Info("shutdown: " + e.Message);
            }
        }
    }
}
