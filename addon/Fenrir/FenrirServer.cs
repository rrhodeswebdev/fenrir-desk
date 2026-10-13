#region Using declarations
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
    // HttpListener routing and the shared-token check. Every request runs on a pool thread; an
    // exception becomes a 500 for that request, never a crashed NinjaTrader.
    public class FenrirServer
    {
        private readonly FenrirConfig config;
        private readonly FenrirBroker broker;
        private readonly HttpListener listener = new HttpListener();

        public FenrirServer(FenrirConfig config, FenrirBroker broker)
        {
            this.config = config;
            this.broker = broker;
        }

        public void Start()
        {
            // "+" so Docker and a Mac host can reach it; needs the netsh urlacl from INSTALL.md
            listener.Prefixes.Add(string.Format("http://+:{0}/", config.Port));
            listener.Start();
            Task.Run(() => AcceptLoop());
        }

        public void Stop()
        {
            listener.Close();
        }

        private async Task AcceptLoop()
        {
            while (listener.IsListening)
            {
                HttpListenerContext ctx;
                try
                {
                    ctx = await listener.GetContextAsync();
                }
                catch
                {
                    return; // listener closed
                }
                ThreadPool.QueueUserWorkItem(_ => Handle(ctx));
            }
        }

        // constant-time compare
        private bool TokenOk(string given)
        {
            string token = config.Token;
            if (given == null || given.Length != token.Length) return false;
            int diff = 0;
            for (int i = 0; i < given.Length; i++) diff |= given[i] ^ token[i];
            return diff == 0;
        }

        private void Handle(HttpListenerContext ctx)
        {
            try
            {
                HttpListenerRequest req = ctx.Request;
                if (!TokenOk(req.Headers["X-Fenrir-Token"]))
                {
                    Reply(ctx, 401, Error("unauthorized"));
                    return;
                }
                object result = Route(req.HttpMethod, req.Url.AbsolutePath, req);
                if (result == null) Reply(ctx, 404, Error("not found"));
                else Reply(ctx, 200, result);
            }
            catch (BadRequest e)
            {
                Reply(ctx, 400, Error(e.Message));
            }
            catch (Exception e)
            {
                FenrirLog.Info("request failed: " + e);
                try
                {
                    Reply(ctx, 500, Error(e.GetType().Name + ": " + e.Message));
                }
                catch
                {
                    // client gone
                }
            }
        }

        private object Route(string method, string path, HttpListenerRequest req)
        {
            if (method == "GET" && path == "/accounts") return broker.Accounts();
            if (method == "GET" && path == "/positions") return broker.Positions(req.QueryString["account"]);
            if (method == "GET" && path == "/orders") return broker.Orders(req.QueryString["account"]);
            if (method == "POST" && path == "/bracket") return broker.Bracket(Body(req));
            if (method == "POST" && path == "/flatten") return broker.Flatten(Body(req));
            if (method == "POST" && path == "/cancel") return broker.Cancel(Body(req));
            if (method == "POST" && path == "/flatten-all") return broker.FlattenAll(Body(req));
            return null;
        }

        private class BadRequest : Exception
        {
            public BadRequest(string message) : base(message) { }
        }

        private static Dictionary<string, object> Body(HttpListenerRequest req)
        {
            string text;
            using (StreamReader r = new StreamReader(req.InputStream, Encoding.UTF8)) text = r.ReadToEnd();
            Dictionary<string, object> body;
            try
            {
                body = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(text);
            }
            catch (Exception)
            {
                body = null;
            }
            if (body == null) throw new BadRequest("body must be a JSON object");
            return body;
        }

        private static Dictionary<string, object> Error(string message)
        {
            return new Dictionary<string, object> { { "error", message } };
        }

        private void Reply(HttpListenerContext ctx, int status, object body)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(body));
            ctx.Response.StatusCode = status;
            ctx.Response.ContentType = "application/json";
            ctx.Response.ContentLength64 = bytes.Length;
            ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
            ctx.Response.Close();
        }
    }
}
