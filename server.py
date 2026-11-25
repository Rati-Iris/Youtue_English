"""Local server for the YouTube English study page.

Usage:
    pip install youtube-transcript-api reportlab anthropic
    python server.py            # then open http://localhost:8000 in Chrome / Edge

API key for automatic lesson generation: api_key.txt next to this file (saved from
the page), or the ANTHROPIC_API_KEY environment variable.

Routes:
    /                          -> index.html
    /api/transcript?v=<id>     -> {"videoId", "title", "lines": [{"text","start","duration"}]}
    POST /api/pdf              -> subtitle log as PDF (body: {title, url, date, rows: [{time, speaker, text}]})
    /lessons/<id>.json         -> lesson file (static)
    GET  /api/settings         -> {"hasKey", "model"}
    GET  /api/lessons          -> [{"id", "title", "mtime"}] of lessons/*.json
    POST /api/settings         -> save API key (body: {apiKey})
    POST /api/generate         -> create lessons/<id>.json with Claude (body: {v})

Phone entry (optional): create lan_pin.txt containing a 4-8 digit PIN next to this file.
The server then also listens on https://<LAN IP>:8443 (needs: pip install cryptography).
Phones must enter the PIN; the API key can only be set from this computer.
    GET  /ca.crt               -> local CA certificate to install / trust on the phone (no PIN)
    POST /api/pin              -> check PIN, set login cookie (body: {pin})
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

FROZEN = getattr(sys, "frozen", False)  # True when packaged as ShadowingStudio.exe (PyInstaller)
# index.html / lessons / transcripts live next to the exe (or next to this script).
ROOT = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
CACHE_DIR = os.path.join(ROOT, "transcripts")
PORT = int(os.environ.get("PORT", "8000"))
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def fetch_title(video_id):
    url = ("https://www.youtube.com/oembed?format=json&url="
           + urllib.parse.quote(f"https://www.youtube.com/watch?v={video_id}", safe=""))
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.load(r).get("title", "")
    except Exception:
        return ""


def fetch_transcript(video_id):
    cache_file = os.path.join(CACHE_DIR, f"{video_id}.json")
    if os.path.exists(cache_file):
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)

    from youtube_transcript_api import YouTubeTranscriptApi

    langs = ["en", "en-US", "en-GB"]
    api = YouTubeTranscriptApi()
    if hasattr(api, "fetch"):  # youtube-transcript-api >= 1.0
        lines = api.fetch(video_id, languages=langs).to_raw_data()
    else:  # older versions
        lines = YouTubeTranscriptApi.get_transcript(video_id, languages=langs)

    data = {"videoId": video_id, "title": fetch_title(video_id), "lines": lines}
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


# ---------------------------------------------------------------- lesson generation (Claude API)
MODEL = "claude-opus-5-5"
KEY_FILE = os.path.join(ROOT, "api_key.txt")  # saved from the web page; never sent back to the browser
LESSON_DIR = os.path.join(ROOT, "lessons")
_gen_lock = threading.Lock()
_generating = set()


def get_api_key():
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def _obj(props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_S, _N, _I = {"type": "string"}, {"type": "number"}, {"type": "integer"}
_SL = {"type": "array", "items": _S}
LESSON_SCHEMA = _obj({
    "level": _S,
    "article": _S,
    "summary_zh": _SL,
    "vocab": {"type": "array", "items": _obj({"word": _S, "pos": _S, "kk": _S, "zh": _S, "quote": _S, "time": _N, "example": _S})},
    "shadowing": {"type": "array", "items": _obj({"text": _S, "start": _N, "end": _N})},
    "mcq": {"type": "array", "items": _obj({"word": _S, "q": _S, "options": _SL, "answer": _I, "explain": _S})},
    "cloze": {"type": "array", "items": _obj({"sentence": _S, "answer": _S, "hint": _S, "explain": _S})},
    "translate": {"type": "array", "items": _obj({"zh": _S, "reference": _S, "keywords": _SL})},
})

SYSTEM_PROMPT = """You create English study material from a YouTube transcript for a learner at CEFR B1-B2 whose first language is Traditional Chinese.
Explanations are written in Traditional Chinese (繁體中文); English content (article, quotes, example sentences, words) stays in English.

The transcript is given one caption line per row as "[start_seconds] text". Lines starting with ">>" mark a change of speaker.

Produce:
- level: "B1–B2".
- article: a 200-350 word graded reading in your own words, B1-B2, keeping the video's main points and flow. Do not copy long passages. Separate paragraphs with a blank line.
- summary_zh: 3-5 sentences in Traditional Chinese summarising the key points.
- vocab: 10-15 items. Prefer useful B1-C1 words and phrases that actually occur in the video, including phrasal verbs, collocations and idioms; skip very basic words and list one form per word family. pos is the part of speech (e.g. "n.", "adj.", "phr. v.", "idiom"); kk is the KK phonetic transcription in brackets; zh is the meaning in Traditional Chinese; quote is the sentence from the video; time is the start_seconds of the caption line where the quote begins, copied exactly from the transcript; example is a new natural example sentence.
- shadowing: 3-5 natural-sounding sentences from the video that contain key vocabulary. start is the start_seconds of the line where the sentence begins; end is the start_seconds of the first line after the sentence ends. Copy the numbers exactly from the transcript.
- mcq: 5 multiple-choice questions on word meaning, each with 4 options in Traditional Chinese (or English where natural); answer is the 0-based index of the correct option, and vary its position across questions; word is the vocabulary item tested; explain says in Traditional Chinese why the answer is right and what the tempting wrong options mean, citing the video time as [m:ss].
- cloze: 5 fill-in-the-blank items built from the article or the video. Mark the blank in sentence with exactly six underscores "______"; answer is the exact missing word and must be one of the key vocabulary items (or its inflected form); hint is the first letter plus the Chinese meaning, e.g. "p…（耐心）"; explain in Traditional Chinese.
- translate: 3 Chinese-to-English translation items whose Chinese sentence (zh) can be translated using key vocabulary; reference is a natural English translation; keywords lists the key vocabulary the learner should use.

Never invent content that is not supported by the transcript."""


def _fix_lesson(lesson):
    """Light validation so a slightly-off model answer still works in the page."""
    for q in lesson.get("mcq", []):
        n = len(q.get("options", []))
        if n and not (0 <= q.get("answer", -1) < n):
            q["answer"] = 0
    for c in lesson.get("cloze", []):
        s, ans = c.get("sentence", ""), c.get("answer", "")
        if "___" not in s and ans:
            c["sentence"] = re.sub(re.escape(ans), "______", s, count=1, flags=re.I)
    lesson["cloze"] = [c for c in lesson.get("cloze", []) if "___" in c.get("sentence", "")]
    for s in lesson.get("shadowing", []):
        if s.get("end", 0) <= s.get("start", 0):
            s["end"] = s.get("start", 0) + 4
    return lesson


def generate_lesson(video_id):
    import anthropic

    key = get_api_key()
    if not key:
        raise PermissionError("no_key")
    data = fetch_transcript(video_id)
    rows = "\n".join("[%.2f] %s" % (l["start"], str(l["text"]).replace("\n", " ")) for l in data["lines"])
    user = f"Video title: {data.get('title') or '(unknown)'}\n\nTranscript:\n{rows}"

    client = anthropic.Anthropic(api_key=key)
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=32000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # if a safety classifier declines, the API retries on a fallback model
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": LESSON_SCHEMA}},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        msg = stream.get_final_message()

    if msg.stop_reason == "refusal":
        raise RuntimeError("Claude 拒絕處理這支影片的內容")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("回應太長被截斷，請再試一次")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    lesson = _fix_lesson(json.loads(text))
    lesson = {"videoId": video_id, "title": data.get("title", ""), **lesson}

    os.makedirs(LESSON_DIR, exist_ok=True)
    path = os.path.join(LESSON_DIR, f"{video_id}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lesson, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return lesson


def list_lessons():
    """[{id, title, mtime}] for every lessons/<id>.json, used to seed the page's history list."""
    out = []
    try:
        names = os.listdir(LESSON_DIR)
    except OSError:
        return out
    for name in names:
        vid = name[:-5] if name.endswith(".json") else ""
        if not VIDEO_ID_RE.match(vid):
            continue
        path = os.path.join(LESSON_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                title = json.load(f).get("title", "")
            out.append({"id": vid, "title": title, "mtime": os.path.getmtime(path)})
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda x: x["mtime"], reverse=True)


def describe_api_error(e):
    import anthropic

    if isinstance(e, anthropic.AuthenticationError):
        return 401, "API key 無效，請重新輸入。"
    if isinstance(e, anthropic.PermissionDeniedError):
        return 403, "這把 API key 沒有權限使用此模型。"
    if isinstance(e, anthropic.RateLimitError):
        return 429, "請求太頻繁，請稍等一分鐘再試。"
    if isinstance(e, anthropic.APIConnectionError):
        return 502, "連不上 Claude API，請檢查網路。"
    if isinstance(e, anthropic.APIStatusError):
        msg = getattr(e, "message", str(e))
        if "credit balance" in msg.lower():
            return 402, "API 帳戶餘額不足，請到 platform.claude.com 儲值。"
        return 502, f"Claude API 錯誤（{e.status_code}）：{msg}"
    return None


# ---------------------------------------------------------------- phone entry (HTTPS + PIN)
LAN_PORT = int(os.environ.get("LAN_PORT", "8443"))
PIN_FILE = os.path.join(ROOT, "lan_pin.txt")  # create it to turn on the phone entry; never served
LAN_DIR = os.path.join(ROOT, "lan")  # CA / server certificates and keys; never served
PIN_RE = re.compile(r"^\d{4,8}$")
MAX_FAILS, LOCK_SECONDS = 5, 300
_fails = {}  # client ip -> (failed attempts, locked until)
_fails_lock = threading.Lock()


def read_pin():
    try:
        with open(PIN_FILE, encoding="utf-8-sig") as f:  # Notepad may add a BOM
            return f.read().strip()
    except OSError:
        return None


def pin_token():
    """Login cookie value: changes when the PIN changes, survives server restarts."""
    pin = read_pin()
    if not pin or not PIN_RE.match(pin):
        return None
    secret_file = os.path.join(LAN_DIR, "secret")
    if not os.path.exists(secret_file):
        os.makedirs(LAN_DIR, exist_ok=True)
        with open(secret_file, "wb") as f:
            f.write(secrets.token_bytes(32))
    with open(secret_file, "rb") as f:
        secret = f.read()
    return hmac.new(secret, pin.encode(), hashlib.sha256).hexdigest()


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # UDP connect sends nothing; it only picks the outgoing interface
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.", "0.")))


def ensure_certs(ips):
    """Create a local CA once (the phone trusts it once) and a server certificate for the current IPs.
    Returns (server_crt, server_key, ca_crt) paths."""
    import datetime
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    os.makedirs(LAN_DIR, exist_ok=True)
    ca_key_path, ca_crt_path = os.path.join(LAN_DIR, "ca.key"), os.path.join(LAN_DIR, "ca.crt")
    srv_key_path, srv_crt_path = os.path.join(LAN_DIR, "server.key"), os.path.join(LAN_DIR, "server.crt")
    now = datetime.datetime.now(datetime.timezone.utc)
    day = datetime.timedelta(days=1)
    pem = serialization.Encoding.PEM

    def save(path, data):
        with open(path, "wb") as f:
            f.write(data)

    def key_bytes(key):
        return key.private_bytes(pem, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())

    def key_usage(**on):
        names = ["digital_signature", "content_commitment", "key_encipherment", "data_encipherment",
                 "key_agreement", "key_cert_sign", "crl_sign", "encipher_only", "decipher_only"]
        return x509.KeyUsage(**{n: on.get(n, False) for n in names})

    if os.path.exists(ca_key_path) and os.path.exists(ca_crt_path):
        with open(ca_key_path, "rb") as f:
            ca_key = serialization.load_pem_private_key(f.read(), None)
        with open(ca_crt_path, "rb") as f:
            ca_crt = x509.load_pem_x509_certificate(f.read())
    else:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Shadowing Studio Local CA")])
        ca_crt = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                  .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
                  .not_valid_before(now - day).not_valid_after(now + 3650 * day)
                  .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                  .add_extension(key_usage(key_cert_sign=True, crl_sign=True), critical=True)
                  .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
                  .sign(ca_key, hashes.SHA256()))
        save(ca_key_path, key_bytes(ca_key))
        save(ca_crt_path, ca_crt.public_bytes(pem))
        for p in (srv_key_path, srv_crt_path):  # signed by the old CA
            if os.path.exists(p):
                os.remove(p)

    # Reuse the server certificate while it covers these IPs and has more than 30 days left.
    if os.path.exists(srv_key_path) and os.path.exists(srv_crt_path):
        with open(srv_crt_path, "rb") as f:
            crt = x509.load_pem_x509_certificate(f.read())
        try:
            san = crt.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            covered = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
        except x509.ExtensionNotFound:
            covered = set()
        if set(ips) <= covered and crt.not_valid_after_utc - now > 30 * day:
            return srv_crt_path, srv_key_path, ca_crt_path

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san = [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    crt = (x509.CertificateBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Shadowing Studio")]))
           .issuer_name(ca_crt.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
           .not_valid_before(now - day).not_valid_after(now + 800 * day)  # iOS rejects > 825 days
           .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
           .add_extension(key_usage(digital_signature=True, key_encipherment=True), critical=True)
           .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
           .add_extension(x509.SubjectAlternativeName(san), critical=False)
           .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
           .sign(ca_key, hashes.SHA256()))
    save(srv_key_path, key_bytes(key))
    save(srv_crt_path, crt.public_bytes(pem))
    return srv_crt_path, srv_key_path, ca_crt_path


LOGIN_PAGE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Shadowing Studio</title>
<style>
body{font-family:-apple-system,system-ui,sans-serif;background:#f4f6f9;color:#1d2733;margin:0;
 min-height:100vh;display:grid;place-items:center;padding:16px;box-sizing:border-box}
form{background:#fff;padding:24px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);
 width:100%;max-width:320px;display:grid;gap:12px}
input,button{font-size:20px;padding:10px;border-radius:8px;border:1px solid #c8d0da}
input{letter-spacing:.3em;text-align:center}button{background:#2453c9;color:#fff;border:0}
#msg{color:#c0392b;min-height:1.2em;font-size:14px}
</style></head><body><form id="f"><h2 style="margin:0">Shadowing Studio</h2>
<div>請輸入電腦上 lan_pin.txt 設定的 PIN</div>
<input id="pin" type="password" inputmode="numeric" autocomplete="current-password" autofocus required>
<button>進入</button><div id="msg"></div></form>
<script>
document.getElementById('f').onsubmit = async e => {
  e.preventDefault();
  const msg = document.getElementById('msg');
  msg.textContent = '';
  try {
    const r = await fetch('/api/pin', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pin: document.getElementById('pin').value})});
    const d = await r.json();
    if (r.ok) location.replace('/'); else msg.textContent = d.error || '登入失敗';
  } catch (err) { msg.textContent = '連線失敗，請確認電腦上的程式還開著。'; }
};
</script></body></html>"""


CJK_RE = re.compile(r"([⺀-鿿　-〿가-힯豈-﫿＀-￯]+)")


def build_pdf(d):
    """Render the subtitle log as an A4 PDF. Returns bytes."""
    import io
    from xml.sax.saxutils import escape
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    cjk_font = "MSung-Light"  # built-in Traditional Chinese CID font, no embedding needed
    if cjk_font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(cjk_font))

    def rich(s):
        # English stays in Helvetica; CJK runs switch to the CID font.
        return CJK_RE.sub(lambda m: f'<font name="{cjk_font}">{m.group(1)}</font>', escape(str(s or "")))

    accent = colors.HexColor("#2453c9")
    muted = colors.HexColor("#5a6677")
    st_title = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=15, leading=20, spaceAfter=4)
    st_meta = ParagraphStyle("meta", fontName="Helvetica", fontSize=9, leading=13, textColor=muted)
    st_time = ParagraphStyle("time", fontName="Courier", fontSize=8, leading=13, textColor=muted)
    st_who = ParagraphStyle("who", fontName="Helvetica-Bold", fontSize=9, leading=13, textColor=accent)
    st_text = ParagraphStyle("text", fontName="Helvetica", fontSize=10, leading=13.5)

    story = [Paragraph(rich(d.get("title") or d.get("videoId", "")), st_title),
             Paragraph(rich(d.get("url", "")), st_meta),
             Paragraph("Date: " + rich(d.get("date", "")), st_meta)]
    if d.get("autoSpeakers"):
        story.append(Paragraph("Speaker labels were detected automatically and may contain errors.", st_meta))
    story.append(Spacer(1, 6 * mm))

    rows, style = [], [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#d9e0e8")),
    ]
    for r in d.get("rows", []):
        rows.append([Paragraph(rich(r.get("time")), st_time),
                     Paragraph(rich(r.get("speaker")), st_who),
                     Paragraph(rich(r.get("text")), st_text)])
    if rows:
        table = Table(rows, colWidths=[16 * mm, 28 * mm, None], repeatRows=0)
        table.setStyle(TableStyle(style))
        story.append(table)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(muted)
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, "Page %d" % doc.page)
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title=str(d.get("title") or "Transcript"))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, status, obj, headers=()):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def allowed_origins(self):
        return {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}

    def is_private(self, fs_path):
        """api_key.txt, lan_pin.txt and the lan/ folder are never served
        (compare file identity: Windows ignores case / trailing dots)."""
        try:
            if not os.path.exists(fs_path):
                return False
            for f in (KEY_FILE, PIN_FILE):
                if os.path.exists(f) and os.path.samefile(fs_path, f):
                    return True
            if os.path.isdir(LAN_DIR):
                p = fs_path
                while True:
                    if os.path.samefile(p, LAN_DIR):
                        return True
                    parent = os.path.dirname(p)
                    if parent == p:
                        return False
                    p = parent
        except OSError:
            return True
        return False

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/transcript":
            vid = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
            if not VIDEO_ID_RE.match(vid):
                return self.send_json(400, {"error": "Invalid video id"})
            try:
                return self.send_json(200, fetch_transcript(vid))
            except Exception as e:
                return self.send_json(502, {"error": f"{type(e).__name__}: {e}"})
        if parsed.path == "/api/settings":
            return self.send_json(200, {"hasKey": bool(get_api_key()), "model": MODEL})
        if parsed.path == "/api/lessons":
            return self.send_json(200, list_lessons())
        if self.is_private(self.translate_path(self.path)):
            return self.send_json(404, {"error": "Not found"})
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/pdf", "/api/settings", "/api/generate"):
            return self.send_json(404, {"error": "Not found"})
        # Only accept JSON from this page: other websites can't send a JSON
        # content type to localhost without a CORS preflight, which we never answer.
        origin = self.headers.get("Origin")
        allowed = self.allowed_origins()
        if not (self.headers.get("Content-Type") or "").startswith("application/json") or (origin and origin not in allowed):
            return self.send_json(403, {"error": "Forbidden"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 10 * 1024 * 1024:
            return self.send_json(400, {"error": "Invalid request size"})
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self.send_json(400, {"error": "Invalid JSON"})
        if path == "/api/settings":
            return self.save_settings(data)
        if path == "/api/generate":
            return self.generate(data)
        try:
            pdf = build_pdf(data)
        except ImportError:
            return self.send_json(500, {"error": "缺少 reportlab，請執行 pip install reportlab 後重開 server.py"})
        except Exception as e:
            return self.send_json(500, {"error": f"{type(e).__name__}: {e}"})
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(pdf)))
        self.end_headers()
        self.wfile.write(pdf)

    def save_settings(self, data):
        key = str(data.get("apiKey", "")).strip()
        if not key.startswith("sk-ant-"):
            return self.send_json(400, {"error": "API key 格式不對，應該以 sk-ant- 開頭。"})
        with open(KEY_FILE, "w", encoding="utf-8") as f:
            f.write(key)
        return self.send_json(200, {"hasKey": True})

    def generate(self, data):
        vid = str(data.get("v", ""))
        if not VIDEO_ID_RE.match(vid):
            return self.send_json(400, {"error": "Invalid video id"})
        with _gen_lock:
            if vid in _generating:
                return self.send_json(409, {"error": "這支影片正在產生中，請稍候。"})
            _generating.add(vid)
        try:
            return self.send_json(200, generate_lesson(vid))
        except PermissionError:
            return self.send_json(401, {"error": "尚未設定 API key。", "code": "no_key"})
        except ImportError:
            return self.send_json(500, {"error": "缺少 anthropic 套件，請執行 pip install anthropic 後重開 server.py"})
        except Exception as e:
            known = describe_api_error(e)
            if known:
                status, msg = known
                return self.send_json(status, {"error": msg, "code": "bad_key" if status == 401 else None})
            return self.send_json(500, {"error": f"{type(e).__name__}: {e}"})
        finally:
            with _gen_lock:
                _generating.discard(vid)


class LanHandler(Handler):
    """HTTPS entry for phones on the same Wi-Fi. Everything except the CA certificate needs the PIN."""
    timeout = 60  # an idle phone connection must not hold a thread forever

    def setup(self):
        # TLS handshake here (handler thread), not in accept(), so one slow client can't block others.
        self.request.settimeout(self.timeout)
        self.request.do_handshake()
        super().setup()

    def allowed_origins(self):
        return {f"https://{self.headers.get('Host', '')}"}

    def authed(self):
        token = pin_token()
        if not token:
            return False
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "ss_pin" and hmac.compare_digest(v.encode(), token.encode()):
                return True
        return False

    def send_bytes(self, ctype, body, headers=()):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/ca.crt":
            with open(os.path.join(LAN_DIR, "ca.crt"), "rb") as f:
                return self.send_bytes("application/x-x509-ca-cert", f.read(),
                                       [("Content-Disposition", 'attachment; filename="ShadowingStudio-CA.crt"')])
        if self.authed():
            return super().do_GET()
        if path in ("/", "/index.html"):
            return self.send_bytes("text/html; charset=utf-8", LOGIN_PAGE.encode("utf-8"))
        return self.send_json(401, {"error": "請重新整理網頁並輸入 PIN。", "code": "need_pin"})

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path == "/api/pin":
            return self.login()
        if not self.authed():
            return self.send_json(401, {"error": "請重新整理網頁並輸入 PIN。", "code": "need_pin"})
        return super().do_POST()

    def save_settings(self, data):
        return self.send_json(403, {"error": f"為了安全，API key 只能在電腦上（http://localhost:{PORT}）設定。"})

    def login(self):
        origin = self.headers.get("Origin")
        if not (self.headers.get("Content-Type") or "").startswith("application/json") or (
                origin and origin not in self.allowed_origins()):
            return self.send_json(403, {"error": "Forbidden"})
        ip, now = self.client_address[0], time.time()
        with _fails_lock:
            count, until = _fails.get(ip, (0, 0))
        if until > now:
            return self.send_json(429, {"error": f"錯誤太多次，請 {int(until - now) // 60 + 1} 分鐘後再試。"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1024:
            return self.send_json(400, {"error": "Invalid request size"})
        try:
            given = str(json.loads(self.rfile.read(length).decode("utf-8")).get("pin", "")).strip()
        except Exception:
            return self.send_json(400, {"error": "Invalid JSON"})
        pin, token = read_pin(), pin_token()
        if token and hmac.compare_digest(given.encode(), pin.encode()):
            with _fails_lock:
                _fails.pop(ip, None)
            cookie = f"ss_pin={token}; Path=/; Max-Age=2592000; HttpOnly; Secure; SameSite=Strict"
            return self.send_json(200, {"ok": True}, [("Set-Cookie", cookie)])
        with _fails_lock:
            count = _fails.get(ip, (0, 0))[0] + 1
            _fails[ip] = (0, now + LOCK_SECONDS) if count >= MAX_FAILS else (count, 0)
        return self.send_json(401, {"error": "PIN 不正確。"})


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second instance bind the same port silently;
    # disable it so a second launch fails and is detected below.
    allow_reuse_address = False


class LanServer(Server):
    def handle_error(self, request, client_address):
        # Phones that don't trust the certificate yet abort the handshake: not worth a traceback.
        if isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def start_lan():
    """Start the phone entry if lan_pin.txt exists. Never stops the computer entry from starting."""
    pin = read_pin()
    if pin is None:
        return
    if not PIN_RE.match(pin):
        print("lan_pin.txt 的 PIN 必須是 4–8 位數字，手機入口未啟動。")
        return
    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("缺少 cryptography 套件（pip install cryptography），手機入口未啟動。")
        return
    ips = local_ips()
    if not ips:
        print("找不到這台電腦的區網 IP，手機入口未啟動。")
        return
    try:
        crt, key, _ = ensure_certs(ips)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        lan = LanServer(("0.0.0.0", LAN_PORT), LanHandler)
    except OSError as e:
        print(f"手機入口未啟動（Port {LAN_PORT} 可能已被使用）：{e}")
        return
    except Exception as e:
        print(f"手機入口未啟動：{type(e).__name__}: {e}")
        return
    lan.socket = ctx.wrap_socket(lan.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=lan.serve_forever, daemon=True).start()
    print("手機入口（同一個 Wi-Fi，需要 PIN）：")
    for ip in ips:
        print(f"    https://{ip}:{LAN_PORT}")
    print(f"  第一次使用：iPhone 用 Safari 開 https://{ips[0]}:{LAN_PORT}/ca.crt 安裝並信任憑證")


if __name__ == "__main__":
    try:
        httpd = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"Port {PORT} 已被使用：可能已經開過一次了，請直接開啟 http://localhost:{PORT}")
        if FROZEN:
            webbrowser.open(f"http://localhost:{PORT}")
            input("按 Enter 關閉此視窗…")
        sys.exit(1)
    print(f"Serving on http://localhost:{PORT}  (使用期間請不要關閉此視窗)")
    start_lan()
    if FROZEN:
        webbrowser.open(f"http://localhost:{PORT}")
    httpd.serve_forever()
