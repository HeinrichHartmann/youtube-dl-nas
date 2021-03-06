import json
import subprocess
from queue import Queue
import io
import sys
from pathlib import Path
import re

from datetime import date
from bottle import run, Bottle, request, static_file, response, redirect, template, get
from threading import Thread
from bottle_websocket import GeventWebSocketServer
from bottle_websocket import websocket
from socket import error

import logging

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s")
L = logging.getLogger(__name__)
L.setLevel(logging.DEBUG)


class WSAddr:
    def __init__(self):
        self.wsClassVal = ""


import bottle

bottle.debug(True)

app = Bottle()
port = 8080
proxy = ""
WS = []


def send(msg):
    for ws in WS.copy():
        try:
            L.debug("> " + msg)
            ws.send(msg)
        except error as e:
            L.debug(f"> ws {ws} failed. Closing.")
            if ws in WS:
                WS.remove(ws)


def pcall(cmd):
    send(f"Running {cmd}")
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="ASCII")
    if p.returncode != 0:
        msg = f"Error executing {cmd}\ncode:{p.returncode}\nout:{p.stdout}\nerr:{p.stderr}"
        send(msg)
        raise Exception(msg)
    return p


@get("/download")
def dl_queue_list():
    return template("./static/template/download.tpl")


_re_date = re.compile("(\d\d\d\d\-\d\d-\d\d).*")


@get("/")
@get("/gallery")
def gallery():
    VIDEO_EXT = {".mkv", ".webm", ".mp4"}
    paths = [
        p
        for p in Path("./videos").glob("**/*")
        if (p.suffix in VIDEO_EXT) and (not p.name.startswith("."))
    ]

    def key(p):
        m = _re_date.match(p.name)
        if m:
            return p.name
        else:
            return "0000-00-00"

    paths = sorted(paths, reverse=True, key=key)
    videos = [{"name": p.name, "src": "/video/" + "/".join(p.parts[1:])} for p in paths]
    return template("./static/template/gallery.tpl", {"videos": videos})


@get("/video/<filepath:path>")
def video(filepath):
    return static_file(filepath, root="./videos")


@get("/websocket", apply=[websocket])
def echo(ws):
    L.debug(f"New WebSocket {ws} total={len(WS)}")
    WS.append(ws)
    # need to receive once so socket gets not closed
    L.debug(ws.receive())
    ws.send(f"Downloads queued {dl_q.qsize()}\n")


@get("/youtube-dl/static/<filepath:path>")
def server_static(filepath):
    return static_file(filepath, root="./static")


@get("/youtube-dl/q", method="GET")
def q_size():
    return {"success": True, "size": json.dumps(list(dl_q.queue))}


@get("/youtube-dl/q", method="POST")
def q_put():
    url = request.json.get("url")
    av = request.json.get("av")
    if "" != url:
        req = {"url": url, "av": av}
        dl_q.put(req)
        send(f"Queued {url}. Total={dl_q.qsize()}")
        if Thr.dl_thread.is_alive() == False:
            thr = Thr()
            thr.restart()
        return {"success": True, "msg": f"Queued download {url}"}
    else:
        return {"success": False, "msg": "Failed"}


def dl_worker():
    L.info("Worker starting")
    while not done:
        item = dl_q.get()
        download(item)
        dl_q.task_done()


def download(req):
    today = date.today().isoformat()
    url = req["url"]
    av = req["av"]
    generate_thumbnail = True
    send(f"Starting download of {url}")
    if av == "A":  # audio only
        cmd = [
            "youtube-dl",
            "--no-progress",
            "--restrict-filenames",
            "--format",
            "bestaudio",
            "-o",
            f"./downloads/{today} %(title)s via %(uploader)s.audio.%(ext)s",
            "--extract-audio",
            "--audio-format",
            "mp3",
            url,
        ]
        generate_thumbnail = False
    else:
        cmd = [
            "youtube-dl",
            "--no-progress",
            "--restrict-filenames",
            "--format",
            "bestvideo[height<=760]+bestaudio",
            # Often sensible video and audio streams are only available separately,
            # so we need to merge the resulting file. Recoding a video to mp4
            # with A+V can take a lot of time, so we opt for an open container format:
            # Option A: Recode Video
            # "--recode-video", "mp4",
            # "--postprocessor-args", "-strict experimental", # allow use of mp4 encoder
            # Option B: Use container format
            # "--merge-output-format", "webm",
            "-o",
            f"./downloads/{today} %(title)s via %(uploader)s.%(ext)s",
            url,
            # "--verbose",
        ]

    send("[youtube-dl] " + " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for line in proc.stdout:
        send("[youtube-dl] " + line.decode("ASCII").rstrip("\n"))
    code = proc.wait()
    proc.stdout.close()
    try:
        if code == 0:
            send("[Finished] " + url + ". Remaining: " + json.dumps(dl_q.qsize()))
        else:
            send("[Failed] " + url)
            return
    except error as e:
        L.error(e)
        send("[Failed]" + str(e))
        return

    if generate_thumbnail:
        p = pcall(cmd + ["--get-filename"])
        fn = p.stdout.rstrip("\n")
        # The filename is not actually accurate. The extension might be wrongly detected.
        # Let's glob this:
        fn = str(list(Path(".").glob(str(Path(fn).with_suffix("")) + "*"))[0])
        p = pcall(
            [
                "ffmpeg",
                "-y",
                "-i",
                fn,
                "-ss",
                "00:00:20.000",
                "-vframes",
                "1",
                fn + ".png",
            ]
        )

    send("Done.")


class Thr:
    def __init__(self):
        self.dl_thread = ""

    def restart(self):
        self.dl_thread = Thread(target=dl_worker)
        self.dl_thread.start()


dl_q = Queue()
done = False
Thr.dl_thread = Thread(target=dl_worker)
Thr.dl_thread.start()

run(host="0.0.0.0", port=port, server=GeventWebSocketServer, reloader=True)

done = True

Thr.dl_thread.join()
