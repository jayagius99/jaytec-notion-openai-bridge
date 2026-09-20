from __future__ import annotations
import json
import uvicorn
from starlette.middleware import Middleware
import reliable_server
import server as legacy_server
from notion_courier_policy import rewrite_call

MAX_BODY = 1_048_576
PATHS = {"/mcp", "/mcp/"}

class CourierBoundary:
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") not in PATHS:
            return await self.app(scope, receive, send)
        headers = dict((k.lower(), v) for k, v in scope.get("headers", []))
        if headers.get(b"content-type", b"").split(b";",1)[0].strip().lower() != b"application/json":
            return await self.app(scope, receive, send)
        chunks=[]; total=0
        while True:
            msg=await receive(); chunks.append(msg)
            if msg.get("type")=="http.request":
                total += len(msg.get("body", b""))
                if total > MAX_BODY:
                    body=b"request body too large"
                    await send({"type":"http.response.start","status":413,"headers":[(b"content-length",str(len(body)).encode())]})
                    return await send({"type":"http.response.body","body":body,"more_body":False})
            if msg.get("type")!="http.request" or not msg.get("more_body",False): break
        body=b"".join(m.get("body",b"") for m in chunks if m.get("type")=="http.request")
        try:
            payload=json.loads(body.decode())
            if isinstance(payload,dict): rewritten=rewrite_call(payload)
            elif isinstance(payload,list): rewritten=[rewrite_call(x) if isinstance(x,dict) else x for x in payload]
            else: rewritten=payload
            new_body=json.dumps(rewritten,separators=(",",":"),sort_keys=True).encode()
        except Exception:
            new_body=body
        new_scope=dict(scope)
        new_headers=[(k,v) for k,v in scope.get("headers",[]) if k.lower()!=b"content-length"]
        new_headers.append((b"content-length",str(len(new_body)).encode()))
        new_scope["headers"]=new_headers
        delivered=False
        async def recv2():
            nonlocal delivered
            if not delivered:
                delivered=True
                return {"type":"http.request","body":new_body,"more_body":False}
            return await receive()
        return await self.app(new_scope,recv2,send)

def create_mcp_app(): return reliable_server.create_mcp_app()
def create_http_app():
    return create_mcp_app().http_app(middleware=[Middleware(CourierBoundary)],stateless_http=True,host_origin_protection=False)
def main(): uvicorn.run(create_http_app(),host="0.0.0.0",port=legacy_server.PORT,log_level="info")
if __name__=="__main__": main()
