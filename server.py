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
"""
import json
import os
import re
import sys
import threading
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

    def send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        # Never serve the saved API key (compare file identity: Windows ignores case / trailing dots).
        fs_path = self.translate_path(self.path)
        try:
            if os.path.exists(KEY_FILE) and os.path.exists(fs_path) and os.path.samefile(fs_path, KEY_FILE):
                return self.send_json(404, {"error": "Not found"})
        except OSError:
            return self.send_json(404, {"error": "Not found"})
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/pdf", "/api/settings", "/api/generate"):
            return self.send_json(404, {"error": "Not found"})
        # Only accept JSON from this page: other websites can't send a JSON
        # content type to localhost without a CORS preflight, which we never answer.
        origin = self.headers.get("Origin")
        allowed = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}
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


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second instance bind the same port silently;
    # disable it so a second launch fails and is detected below.
    allow_reuse_address = False


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
    if FROZEN:
        webbrowser.open(f"http://localhost:{PORT}")
    httpd.serve_forever()
