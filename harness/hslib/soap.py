"""GM commands over worldserver SOAP (urn:AC executeCommand)."""
import base64
import html
import re
import urllib.error
import urllib.request

from . import paths as P

ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns1="urn:AC">'
    "<SOAP-ENV:Body><ns1:executeCommand><command>{cmd}</command></ns1:executeCommand></SOAP-ENV:Body>"
    "</SOAP-ENV:Envelope>"
)


class GMResult:
    def __init__(self, ok: bool, output: str, http_status: int, command: str):
        self.ok, self.output, self.http_status, self.command = ok, output, http_status, command

    def evidence(self) -> dict:
        return {"gm": self.command, "ok": self.ok, "http": self.http_status, "output": self.output[-2000:]}

    def __repr__(self):
        return f"GMResult(ok={self.ok}, http={self.http_status}, output={self.output!r})"


def gm(command: str, user=P.GM_USER, password=P.GM_PASS, timeout=60) -> GMResult:
    """Execute a GM command. ok=False on SOAP fault (command failed/unknown) or HTTP auth errors.
    Raises OSError/URLError only when the SOAP endpoint is unreachable."""
    cmd = command.lstrip(".")
    req = urllib.request.Request(
        f"http://{P.HOST}:{P.SOAP_PORT}/",
        data=ENVELOPE.format(cmd=html.escape(cmd)).encode(),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body, status = r.read().decode(errors="replace"), r.status
    except urllib.error.HTTPError as e:
        body, status = e.read().decode(errors="replace"), e.code
    m = re.search(r"<result>(.*?)</result>", body, re.S)
    if status == 200 and m:
        return GMResult(True, html.unescape(m.group(1)), status, cmd)
    f = re.search(r"<faultstring>(.*?)</faultstring>", body, re.S) or re.search(r"<detail>(.*?)</detail>", body, re.S)
    return GMResult(False, html.unescape(f.group(1)) if f else body.strip()[:2000], status, cmd)
