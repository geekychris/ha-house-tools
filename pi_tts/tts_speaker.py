#!/usr/bin/env python3
"""Tiny HTTP TTS server for pi-sf.

POST /say {"text": "hello"} -> fetches Google Translate TTS mp3, plays
it through the Pi's audio output via ffplay. No system TTS install
needed; just Python + ffplay (already there).

Audio routing: when this runs as a systemd service (no graphical
session, no XDG_RUNTIME_DIR), ffplay's SDL backend can't reach
PipeWire and falls back to a wrong/missing ALSA default. We force
SDL to use ALSA directly and point AUDIODEV at the HDMI port the
monitor is actually on (the pi-sf monitor is on vc4hdmi1; HDMI 0
isn't connected). Override AUDIODEV via env if your wiring differs.
"""
import http.server, json, os, socket, subprocess, sys, urllib.parse, urllib.request

PORT = 5006
PLAY_ENV = {
    **os.environ,
    "SDL_AUDIODRIVER": os.environ.get("SDL_AUDIODRIVER", "alsa"),
    "AUDIODEV": os.environ.get("AUDIODEV", "plughw:CARD=vc4hdmi1"),
}

class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != '/say':
            self.send_error(404); return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            body = json.loads(self.rfile.read(length).decode('utf-8'))
            text = (body.get('text') or '').strip()
        except Exception as e:
            self.send_error(400, str(e)); return
        if not text:
            self.send_error(400, 'missing text'); return
        url = ('https://translate.google.com/translate_tts'
               f'?ie=UTF-8&client=tw-ob&tl=en&q={urllib.parse.quote(text)}')
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                mp3 = r.read()
        except Exception as e:
            self.send_error(502, f'gtts fetch failed: {e}'); return
        try:
            subprocess.run(['ffplay', '-nodisp', '-autoexit',
                            '-loglevel', 'error', '-'],
                           input=mp3, env=PLAY_ENV, check=False, timeout=60)
        except Exception as e:
            self.send_error(500, f'playback failed: {e}'); return
        self.send_response(200); self.end_headers(); self.wfile.write(b'ok\n')

    def do_GET(self):
        if self.path == '/healthz':
            self.send_response(200); self.end_headers(); self.wfile.write(b'ok\n')
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):  # quiet access log
        sys.stderr.write(f'[tts] {fmt % args}\n')

if __name__ == '__main__':
    print(f'Listening on 0.0.0.0:{PORT} (host={socket.gethostname()})', flush=True)
    http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
