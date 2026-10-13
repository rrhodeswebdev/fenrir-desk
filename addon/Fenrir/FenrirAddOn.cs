#region Using declarations
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Threading;
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
        public int FlattenOnDisconnectSeconds; // 0 = off (null in the file)
        public TimeSpan FlattenAt = new TimeSpan(16, 40, 0); // America/New_York; configurable only to test it

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
            if (raw.TryGetValue("flatten_at", out v))
            {
                TimeSpan at;
                if (v is string && TimeSpan.TryParseExact((string)v, "hh\\:mm", CultureInfo.InvariantCulture, out at))
                    config.FlattenAt = at;
                else
                    FenrirLog.Alert("flatten_at must be \"HH:MM\" (New York time); using 16:40");
            }
            if (raw.TryGetValue("flatten_on_disconnect_seconds", out v) && v != null)
            {
                if (v is int && (int)v > 0) config.FlattenOnDisconnectSeconds = (int)v;
                else FenrirLog.Alert("flatten_on_disconnect_seconds must be null or a whole number of seconds > 0; it is off");
            }
            return config;
        }
    }

    public static class FenrirLog
    {
        // NinjaScript Output window, tab 1. Logging never throws: it runs inside catch blocks.
        public static void Info(string message)
        {
            try
            {
                NinjaTrader.Code.Output.Process("Fenrir: " + message, PrintTo.OutputTab1);
            }
            catch
            {
            }
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
        private Timer flattenTimer;

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
            }
            catch (Exception e)
            {
                config = new FenrirConfig();
                FenrirLog.Alert("could not read fenrir-addon.json (" + e.Message + "): no account is managed");
            }
            if (config.ManagedAccounts.Count == 0)
                FenrirLog.Alert("managed_accounts is empty in fenrir-addon.json: Fenrir will not see or touch any account");

            // first and on its own: the 16:40 flatten runs even when the server cannot start
            try
            {
                broker = new FenrirBroker(config);
                broker.Start();
                flattenTimer = new Timer(_ => CheckFlatten(), null, 0, 15000);
                FenrirLog.Info(string.Format("flattening managed accounts daily at {0:hh\\:mm} New York time", config.FlattenAt));
            }
            catch (Exception e)
            {
                FenrirLog.Alert("the daily New York flatten failed to start: " + e);
            }

            try
            {
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

        // Every 15 s from flatten_at to 20 minutes later, New York time whatever the PC's zone (DST
        // included): flatten and cancel everything on the managed accounts, Python connected or not.
        // Repeating through the window also closes anything opened late, by anyone.
        private void CheckFlatten()
        {
            try
            {
                if (broker.SecondsSinceSweep > 5)
                    FenrirLog.Alert(string.Format("the broker thread has not run for {0:0} s: orders and the New York flatten are stuck", broker.SecondsSinceSweep));
                DateTime ny = TimeZoneInfo.ConvertTimeBySystemTimeZoneId(DateTime.UtcNow, "Eastern Standard Time");
                if (ny.TimeOfDay >= config.FlattenAt && ny.TimeOfDay < config.FlattenAt + TimeSpan.FromMinutes(20))
                    broker.FlattenManaged(string.Format("{0:hh\\:mm} New York flatten", config.FlattenAt));
            }
            catch (Exception e)
            {
                FenrirLog.Alert("New York flatten check failed, retrying in 15 s: " + e.Message);
            }
        }

        private void Shutdown()
        {
            if (!started) return; // instances made only to list add-ons never started
            try
            {
                if (flattenTimer != null) flattenTimer.Dispose();
                if (server != null) server.Stop();
                if (broker != null) broker.Stop();
            }
            catch (Exception e)
            {
                FenrirLog.Info("shutdown: " + e.Message);
            }
        }
    }
}
