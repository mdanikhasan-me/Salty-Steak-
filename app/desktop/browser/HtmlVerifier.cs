using System.Text;
using System.Text.Json.Nodes;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace SaltyPotatoAI.Browser;

// A fresh, disposable renderer. No user's browser profile or native host bridge.
internal static class HtmlVerifier
{
    internal static async Task<JsonObject> Verify(JsonObject payload)
    {
        var html = payload["html"]?.GetValue<string>() ?? "";
        if (Encoding.UTF8.GetByteCount(html) > 1_000_000)
            throw new ArgumentException("HTML exceeds the verification size limit.");
        var profile = Path.Combine(Path.GetTempPath(), "salty-html-check-" + Guid.NewGuid().ToString("N"));
        using var window = new Form { ClientSize = new Size(1000, 760), ShowInTaskbar = false,
            StartPosition = FormStartPosition.Manual, Location = new Point(-32000, -32000) };
        using var view = new WebView2 { Dock = DockStyle.Fill };
        window.Controls.Add(view); window.Show();
        var environment = await CoreWebView2Environment.CreateAsync(userDataFolder: profile);
        await view.EnsureCoreWebView2Async(environment);
        var core = view.CoreWebView2;
        core.Settings.AreHostObjectsAllowed = false;
        core.Settings.IsWebMessageEnabled = false;
        core.Settings.AreDefaultScriptDialogsEnabled = false;
        core.Settings.AreDevToolsEnabled = false;
        core.PermissionRequested += (_, e) => { e.State = CoreWebView2PermissionState.Deny; e.Handled = true; };
        core.NewWindowRequested += (_, e) => e.Handled = true;
        core.DownloadStarting += (_, e) => e.Cancel = true;
        core.NavigationStarting += (_, e) => { if (e.Uri != "https://salty-verification.invalid/") e.Cancel = true; };
        core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All);
        var blocked = new List<string>();
        core.WebResourceRequested += (_, e) => {
            var document = e.Request.Uri == "https://salty-verification.invalid/" && e.ResourceContext == CoreWebView2WebResourceContext.Document;
            if (!document && blocked.Count < 20) blocked.Add(e.Request.Uri[..Math.Min(200,e.Request.Uri.Length)]);
            var body = new MemoryStream(Encoding.UTF8.GetBytes(document ? html : ""));
            e.Response = environment.CreateWebResourceResponse(body, document ? 200 : 403, document ? "OK" : "Blocked",
                "Content-Type: text/html; charset=utf-8\r\nContent-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; frame-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'\r\n");
        };
        await core.AddScriptToExecuteOnDocumentCreatedAsync("""
            (()=>{const errors=[],limitations=[],targets=[];Object.defineProperty(window,'__saltyCheck',{value:{errors,limitations,targets},writable:false});
            addEventListener('error',e=>errors.push(String(e.message).slice(0,500)));
            addEventListener('securitypolicyviolation',e=>limitations.push('Blocked resource or operation: '+String(e.violatedDirective).slice(0,150)));
            addEventListener('unhandledrejection',e=>errors.push(String(e.reason).slice(0,500)));
            const original=CanvasRenderingContext2D.prototype.fillText;
            CanvasRenderingContext2D.prototype.fillText=function(text,x,y,...rest){
              if(/^(start|play|begin)(\b|\s)/i.test(String(text).trim())){
                const m=this.measureText(String(text)),r=this.canvas.getBoundingClientRect(),t=this.getTransform();
                const point=new DOMPoint(Number(x)+(m.actualBoundingBoxRight-m.actualBoundingBoxLeft)/2,Number(y)+(m.actualBoundingBoxDescent-m.actualBoundingBoxAscent)/2).matrixTransform(t);
                targets.push({label:String(text).slice(0,100),x:r.left+point.x*r.width/this.canvas.width,y:r.top+point.y*r.height/this.canvas.height,kind:'canvas',at:performance.now()});
                if(targets.length>40)targets.shift();
              }
              return original.call(this,text,x,y,...rest);
            };})();
            """);
        var loaded = new TaskCompletionSource<bool>();
        core.NavigationCompleted += (_, e) => loaded.TrySetResult(e.IsSuccess);
        core.Navigate("https://salty-verification.invalid/");
        if (await Task.WhenAny(loaded.Task, Task.Delay(10000)) != loaded.Task || !await loaded.Task)
            throw new TimeoutException("Verification page did not load.");
        await Task.Delay(350);
        const string snapshot = """
            (()=>{const visible=e=>!!(e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');
            const canvases=[...document.querySelectorAll('canvas')];
            return {text:document.body.innerText.slice(0,20000),canvas:canvases.map(c=>{try{return c.toDataURL()}catch{return 'unreadable'}}).join('|'),
            controls:[...document.querySelectorAll('button,input[type=button],input[type=submit],[role=button]')].filter(visible).map(e=>e.innerText||e.value||e.getAttribute('aria-label')||'').slice(0,30),
            targets:window.__saltyCheck.targets.filter(t=>performance.now()-t.at<1500).map(t=>({label:t.label,x:t.x,y:t.y,kind:t.kind})),
            errors:window.__saltyCheck.errors.slice(0,10),limitations:window.__saltyCheck.limitations.slice(0,10)}})()
            """;
        async Task<JsonObject> Read() => JsonNode.Parse(await core.ExecuteScriptAsync(snapshot))!.AsObject();
        var baseline = await Read();
        await Task.Delay(180);
        var before = await Read();
        var target = JsonNode.Parse(await core.ExecuteScriptAsync("""
            (()=>{const list=[...document.querySelectorAll('button,input[type=button],input[type=submit],[role=button]')];
            const e=list.find(e=>e.getClientRects().length&&!e.disabled&&/^(start|play|begin)(\b|\s)/i.test((e.innerText||e.value||e.getAttribute('aria-label')||'').trim()));
            if(e){const r=e.getBoundingClientRect();return {label:(e.innerText||e.value||e.getAttribute('aria-label')).slice(0,100),x:r.left+r.width/2,y:r.top+r.height/2,kind:'dom'}}
            return [...window.__saltyCheck.targets].reverse().find(t=>performance.now()-t.at<1500&&t.x>=0&&t.y>=0&&t.x<innerWidth&&t.y<innerHeight)||null})()
            """))?.AsObject();
        var click = target?["label"]?.GetValue<string>();
        if (target is not null)
        {
            var x = target["x"]!.GetValue<double>(); var y = target["y"]!.GetValue<double>();
            foreach (var type in new[] { "mousePressed", "mouseReleased" })
                await core.CallDevToolsProtocolMethodAsync("Input.dispatchMouseEvent", new JsonObject {
                    ["type"]=type, ["x"]=x, ["y"]=y, ["button"]="left", ["clickCount"]=1
                }.ToJsonString());
        }
        await Task.Delay(1700);
        var after = await Read();
        await core.CallDevToolsProtocolMethodAsync("Input.dispatchKeyEvent", "{\"type\":\"keyDown\",\"key\":\"ArrowRight\",\"code\":\"ArrowRight\",\"windowsVirtualKeyCode\":39}");
        await Task.Delay(250);
        await core.CallDevToolsProtocolMethodAsync("Input.dispatchKeyEvent", "{\"type\":\"keyUp\",\"key\":\"ArrowRight\",\"code\":\"ArrowRight\",\"windowsVirtualKeyCode\":39}");
        var final = await Read();
        var changed = before["text"]?.ToJsonString() != after["text"]?.ToJsonString()
            || (baseline["canvas"]?.ToJsonString() == before["canvas"]?.ToJsonString() && before["canvas"]?.ToJsonString() != after["canvas"]?.ToJsonString())
            || before["controls"]?.ToJsonString() != after["controls"]?.ToJsonString();
        var errors = final["errors"]!.AsArray();
        var issues = new JsonArray();
        foreach (var error in errors) issues.Add(error?.GetValue<string>());
        var limitations = final["limitations"]!.AsArray().DeepClone().AsArray();
        var environmentLimited = limitations.Count > 0 || blocked.Count > 0;
        if (click != null && !changed) limitations.Add("Start/Play was clicked, but no reliable state change was observed; the test cannot confirm startup.");
        if (blocked.Count > 0) limitations.Add("External resources were blocked; this environment cannot verify their behavior.");
        var requiresStart = payload["require_start"]?.GetValue<bool>() == true;
        if (requiresStart && click == null) limitations.Add("No supported Start/Play target was found. Canvas/image/keyboard controls may still work; do not change the UI solely to satisfy this detector.");
        var status = environmentLimited ? "unverified" : issues.Count > 0 ? "failed" : limitations.Count > 0 ? "unverified" : "passed";
        var result = new JsonObject {
            ["status"] = status, ["issues"] = issues, ["limitations"] = limitations,
            ["start_control"] = click, ["start_changed_page"] = click != null && changed,
            ["interaction_method"] = target?["kind"]?.GetValue<string>(),
            ["pointer_input"] = target is not null ? "browser_mouse_input" : "not_dispatched",
            ["keyboard_dispatched"] = true, ["network_blocked"] = true,
            ["scope"] = "Browser smoke check: load, runtime errors, Start/Play effect, right-arrow dispatch. Gameplay correctness and all controls are not proven."
        };
        // Dispose the renderer before attempting best-effort ephemeral-profile cleanup.
        view.Dispose(); window.Close();
        try { Directory.Delete(profile, true); } catch (IOException) { } catch (UnauthorizedAccessException) { }
        return result;
    }
}
