"""Drive a real WebAuthn registration + login through Chrome.

Chrome's DevTools Protocol can attach a *virtual* platform authenticator that
behaves like Touch ID without a fingerprint. That is the only way to prove this
flow end to end without a human thumb, and it exercises the real browser API
and the real server verification.
"""
import asyncio, json, subprocess, sys, time, urllib.request
import websockets

PORT = 9333
APP = "http://localhost:8000/"  # must be localhost: an IP cannot be a WebAuthn RP ID

chrome = subprocess.Popen([
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "--headless=new", "--disable-gpu", f"--remote-debugging-port={PORT}",
    "--user-data-dir=/tmp/roswell-cdp", "--no-first-run", APP,
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def target():
    for _ in range(60):
        try:
            pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
            for p in pages:
                if p.get("type") == "page" and "8000" in p.get("url", ""):
                    return p["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit("no page target")

async def main():
    ws_url = target()
    async with websockets.connect(ws_url, max_size=20_000_000) as ws:
        n = 0
        async def send(method, params=None):
            nonlocal n
            n += 1
            await ws.send(json.dumps({"id": n, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error']}")
                    return msg.get("result", {})

        async def js(expr):
            r = await send("Runtime.evaluate", {
                "expression": expr, "awaitPromise": True, "returnByValue": True})
            if "exceptionDetails" in r:
                return {"error": str(r["exceptionDetails"].get("exception", {}).get("description"))[:200]}
            return r["result"].get("value")

        await send("Runtime.enable")
        await send("WebAuthn.enable")
        auth_id = (await send("WebAuthn.addVirtualAuthenticator", {
            "options": {
                "protocol": "ctap2", "transport": "internal",
                "hasResidentKey": True, "hasUserVerification": True,
                "isUserVerified": True, "automaticPresenceSimulation": True,
            }
        }))["authenticatorId"]
        print(f"virtual platform authenticator attached: {auth_id[:12]}…")

        await asyncio.sleep(3)
        out = {}
        out["status"] = await js("fetch('/api/auth/status').then(r=>r.json())")

        # A fresh password so the run is self-contained.
        out["setup"] = await js("""
          fetch('/api/auth/setup',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({password:'cdp-probe-password'})})
            .then(r=>r.status)""")

        out["register"] = await js("registerBiometric().then(()=>'ok').catch(e=>'FAIL: '+e)")
        out["after_register"] = await js(
            "fetch('/api/auth/status').then(r=>r.json()).then(d=>d.biometric_registered)")

        out["logout"] = await js("fetch('/api/auth/logout',{method:'POST'}).then(r=>r.status)")
        out["locked_now"] = await js(
            "fetch('/api/watchlist').then(r=>r.status)")

        # The real thing: authenticate with the virtual fingerprint.
        out["biometric_login"] = await js("""
          (async () => {
            const o = await (await fetch('/api/auth/biometric/login/begin',{method:'POST'})).json();
            const a = await navigator.credentials.get({publicKey:{
              challenge: b64urlToBytes(o.challenge), rpId: o.rpId,
              userVerification: 'required',
              allowCredentials: (o.allowCredentials||[]).map(c=>({id:b64urlToBytes(c.id),type:'public-key'})),
            }});
            const r = await fetch('/api/auth/biometric/login/finish',{method:'POST',
              headers:{'Content-Type':'application/json'},
              body: JSON.stringify({id:a.id, rawId:bytesToB64url(a.rawId), type:a.type,
                response:{clientDataJSON:bytesToB64url(a.response.clientDataJSON),
                  authenticatorData:bytesToB64url(a.response.authenticatorData),
                  signature:bytesToB64url(a.response.signature),
                  userHandle:a.response.userHandle?bytesToB64url(a.response.userHandle):null}})});
            return r.status + ' ' + JSON.stringify(await r.json());
          })().catch(e => 'FAIL: ' + e)""")

        out["unlocked_after_biometric"] = await js(
            "fetch('/api/watchlist').then(r=>r.status)")
        print(json.dumps(out, indent=2)[:1800])

try:
    asyncio.run(main())
finally:
    chrome.terminate()
