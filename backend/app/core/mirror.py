"""Read-only guard for the cloud copy of the clinic database (mirror_mode).

The real clinic data lives on the clinic's offline server and is uploaded here
as a whole-file copy, so anything written to the copy would be lost at the next
upload. Only reading (and logging in) is allowed.
"""
import json

ALLOWED_WRITES = ("/api/v1/auth/",)
MESSAGE = "Clinic data (cloud copy) — ကြည့်ရုံသာ ရပါတယ်။ ပြင်ချင်ရင် Clinic computer မှာ ပြင်ပါ။"


class ReadOnlyMirrorMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope["method"] not in ("GET", "HEAD", "OPTIONS")
            and not scope["path"].startswith(ALLOWED_WRITES)
        ):
            body = json.dumps({"detail": MESSAGE}).encode()
            await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
