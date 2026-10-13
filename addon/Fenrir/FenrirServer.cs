#region Using declarations
using System;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
    // HttpListener routing and the shared-token check. Every request runs on a pool thread; an
    // exception becomes a 500 for that request, never a crashed NinjaTrader.
    //
    // /stream: NinjaTrader's event handlers only drop a message in one bounded queue; one sender
    // thread writes it to every client, and a client that takes over 2 s to take a message is
    // dropped. So NinjaTrader's threads never wait on the network.
    public class FenrirServer
    {
        private readonly FenrirConfig config;
        private readonly FenrirBroker broker;
        private readonly HttpListener listener = new HttpListener();
        private readonly List<WebSocket> sockets = new List<WebSocket>();
        private readonly BlockingCollection<string> outbox = new BlockingCollection<string>(1000);
        private DateTime noClientSince = DateTime.UtcNow; // under lock (sockets)
        private Timer beat;

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
            broker.Publish = Enqueue;
            Task.Run(() => AcceptLoop());
            Thread sender = new Thread(SendLoop);
            sender.IsBackground = true;
            sender.Name = "Fenrir stream";
            sender.Start();
            beat = new Timer(_ => Beat(), null, 0, 2000);
        }

        public void Stop()
        {
            if (beat != null) beat.Dispose();
            broker.Publish = delegate { };
            outbox.CompleteAdding();
            listener.Close();
            lock (sockets) foreach (WebSocket ws in sockets) Drop(ws);
        }

        private void Enqueue(Dictionary<string, object> msg)
        {
            // the client rejects NaN and infinity
            if (msg.Values.Any(v => v is double && !FenrirBroker.Finite((double)v)))
            {
                FenrirLog.Info("not streamed, non-finite number: " + msg["type"]);
                return;
            }
            try
            {
                if (!outbox.TryAdd(new JavaScriptSerializer().Serialize(msg)))
                    FenrirLog.Info("stream queue full, dropped a " + msg["type"] + " message");
            }
            catch (InvalidOperationException)
            {
                // stopped
            }
        }

        // every 2 s: heartbeat, one account message per managed account, flatten_on_disconnect_seconds;
        // each in its own try, so one failing never stops the others
        private void Beat()
        {
            try
            {
                Enqueue(new Dictionary<string, object> { { "type", "heartbeat" }, { "ts", FenrirBroker.Utc(DateTime.UtcNow) } });
                foreach (object row in broker.Accounts())
                {
                    Dictionary<string, object> msg = new Dictionary<string, object>((Dictionary<string, object>)row);
                    msg["type"] = "account";
                    Enqueue(msg);
                }
            }
            catch (Exception e)
            {
                FenrirLog.Info("heartbeat: " + e.Message);
            }
            try
            {
                if (config.FlattenOnDisconnectSeconds > 0)
                {
                    double alone;
                    lock (sockets) alone = sockets.Count > 0 ? 0 : (DateTime.UtcNow - noClientSince).TotalSeconds;
                    if (alone >= config.FlattenOnDisconnectSeconds)
                        broker.FlattenHeld(string.Format("no /stream client for {0:0} s", alone));
                }
            }
            catch (Exception e)
            {
                FenrirLog.Alert("flatten_on_disconnect_seconds check failed: " + e.Message);
            }
        }

        // a dedicated thread: nothing may escape it
        private void SendLoop()
        {
            try
            {
                foreach (string text in outbox.GetConsumingEnumerable())
                {
                    ArraySegment<byte> bytes = new ArraySegment<byte>(Encoding.UTF8.GetBytes(text));
                    WebSocket[] targets;
                    lock (sockets) targets = sockets.ToArray();
                    foreach (WebSocket ws in targets)
                    {
                        try
                        {
                            if (!ws.SendAsync(bytes, WebSocketMessageType.Text, true, CancellationToken.None).Wait(2000))
                                Drop(ws); // stalled: drop it rather than hold everyone up
                        }
                        catch
                        {
                            Drop(ws);
                        }
                    }
                }
            }
            catch (Exception e)
            {
                FenrirLog.Alert("stream sender stopped: " + e.Message);
            }
        }

        private static void Drop(WebSocket ws)
        {
            try
            {
                ws.Abort();
            }
            catch
            {
            }
        }

        // the token is in ?token= (contract); a wrong one is refused before the upgrade
        private async Task HandleSocket(HttpListenerContext ctx)
        {
            WebSocket ws = null;
            try
            {
                if (!TokenOk(ctx.Request.QueryString["token"]))
                {
                    Reply(ctx, 401, Error("unauthorized"));
                    return;
                }
                ws = (await ctx.AcceptWebSocketAsync(null)).WebSocket;
                lock (sockets) sockets.Add(ws);
                byte[] buffer = new byte[1024];
                while (ws.State == WebSocketState.Open)
                {
                    // nothing is expected from the client; reading answers its pings and close
                    WebSocketReceiveResult r = await ws.ReceiveAsync(new ArraySegment<byte>(buffer), CancellationToken.None);
                    if (r.MessageType == WebSocketMessageType.Close) break;
                }
            }
            catch
            {
                // client went away or aborted by the sender
            }
            finally
            {
                if (ws != null)
                {
                    lock (sockets)
                    {
                        sockets.Remove(ws);
                        if (sockets.Count == 0) noClientSince = DateTime.UtcNow;
                    }
                    try
                    {
                        ws.Dispose();
                    }
                    catch
                    {
                    }
                }
            }
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

        // A pool thread: nothing may escape it, since an unhandled exception there ends NinjaTrader.
        private void Handle(HttpListenerContext ctx)
        {
            int status;
            object body;
            try
            {
                HttpListenerRequest req = ctx.Request;
                if (req.Url.AbsolutePath == "/stream" && req.IsWebSocketRequest)
                {
                    Task open = HandleSocket(ctx); // runs until the client leaves; catches its own errors
                    return;
                }
                if (!TokenOk(req.Headers["X-Fenrir-Token"]))
                {
                    status = 401;
                    body = Error("unauthorized");
                }
                else
                {
                    object result = Route(req.HttpMethod, req.Url.AbsolutePath, req);
                    status = result == null ? 404 : 200;
                    body = result ?? Error("not found");
                }
            }
            catch (BadRequest e)
            {
                status = 400;
                body = Error(e.Message);
            }
            catch (Exception e)
            {
                FenrirLog.Info("request failed: " + e);
                status = 500;
                body = Error(e.GetType().Name + ": " + e.Message);
            }
            try
            {
                Reply(ctx, status, body);
            }
            catch
            {
                // client gone
                try
                {
                    ctx.Response.Abort();
                }
                catch
                {
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
