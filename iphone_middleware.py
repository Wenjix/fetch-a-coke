#!/usr/bin/env python3
# Copyright 2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from dimos.experimental.fetch.policy import FetchPolicy, FetchPolicyConfig
from dimos.experimental.fetch.record3d_source import Record3DSource
from dimos.utils.logging_config import setup_logger
from dimos.utils.path_utils import get_project_root
from dimos.web.dimos_interface.api.server import FastAPIServer

logger = setup_logger()

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_PORT = 8455


class FetchIphoneMiddleware:
    """HTTPS phone-camera middleware for testing the Fetch behavior."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        model: str = "gpt-5-mini",
        tts_model: str = "tts-1",
        tts_voice: str = "echo",
        record3d: bool = False,
        record3d_device_index: int = 0,
    ) -> None:
        self.host = host
        self.port = port
        self.tts_model = tts_model
        self.tts_voice = tts_voice
        self.policy = FetchPolicy(FetchPolicyConfig(model=model))
        self.server = FastAPIServer(
            dev_name="Fetch iPhone Middleware",
            edge_type="Bidirectional",
            host=host,
            port=port,
        )
        self._openai_client = OpenAI()
        self._record3d_source = Record3DSource(record3d_device_index) if record3d else None
        self._setup_routes()

    def _setup_routes(self) -> None:
        self.server.app.router.routes = [
            route for route in self.server.app.router.routes if getattr(route, "path", None) != "/"
        ]

        @self.server.app.get("/", response_class=HTMLResponse)
        @self.server.app.get("/fetch", response_class=HTMLResponse)
        async def index() -> HTMLResponse:
            return HTMLResponse(
                content=(STATIC_DIR / "index.html").read_text(),
                headers={"Cache-Control": "no-store"},
            )

        @self.server.app.get("/health")
        async def health() -> dict[str, Any]:
            return {"ok": True, "service": "fetch-iphone", "port": self.port}

        @self.server.app.get("/record3d/status")
        async def record3d_status() -> dict[str, Any]:
            if self._record3d_source is None:
                return {"enabled": False}
            return {"enabled": True, **self._record3d_source.status()}

        @self.server.app.post("/record3d/restart")
        async def record3d_restart() -> dict[str, Any]:
            if self._record3d_source is None:
                return {"enabled": False, "restarted": False}
            self._record3d_source.restart()
            return {"enabled": True, "restarted": True, **self._record3d_source.status()}

        @self.server.app.get("/record3d/latest.jpg")
        async def record3d_latest_jpg() -> Response:
            if self._record3d_source is None:
                return JSONResponse({"error": "Record3D is not enabled"}, status_code=404)
            frame = self._record3d_source.latest()
            if frame is None:
                return JSONResponse({"error": "No Record3D frame received yet"}, status_code=404)
            return Response(content=frame.jpeg_bytes, media_type="image/jpeg")

        @self.server.app.get("/record3d/latest-depth.jpg")
        async def record3d_latest_depth_jpg() -> Response:
            if self._record3d_source is None:
                return JSONResponse({"error": "Record3D is not enabled"}, status_code=404)
            frame = self._record3d_source.latest()
            if frame is None:
                return JSONResponse({"error": "No Record3D frame received yet"}, status_code=404)
            return Response(content=frame.depth_jpeg_bytes, media_type="image/jpeg")

        def stream_record3d_jpegs(frame_attr: str) -> StreamingResponse | JSONResponse:
            if self._record3d_source is None:
                return JSONResponse({"error": "Record3D is not enabled"}, status_code=404)

            async def stream_frames() -> Any:
                last_captured_at = 0.0
                while True:
                    frame = self._record3d_source.latest()
                    if frame is not None and frame.captured_at != last_captured_at:
                        last_captured_at = frame.captured_at
                        jpeg_bytes = getattr(frame, frame_attr)
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            b"Cache-Control: no-cache\r\n\r\n"
                            + jpeg_bytes
                            + b"\r\n"
                        )
                    await asyncio.sleep(0.03)

            return StreamingResponse(
                stream_frames(),
                media_type="multipart/x-mixed-replace; boundary=frame",
                headers={"Cache-Control": "no-cache"},
            )

        @self.server.app.get("/record3d/stream.mjpg")
        async def record3d_stream() -> Any:
            return stream_record3d_jpegs("jpeg_bytes")

        @self.server.app.get("/record3d/stream-depth.mjpg")
        async def record3d_depth_stream() -> Any:
            return stream_record3d_jpegs("depth_jpeg_bytes")

        @self.server.app.post("/record3d/analyze")
        async def record3d_analyze() -> Any:
            if self._record3d_source is None:
                return JSONResponse({"error": "Record3D is not enabled"}, status_code=404)
            frame = self._record3d_source.latest()
            if frame is None:
                return JSONResponse({"error": "No Record3D frame received yet"}, status_code=404)
            return await asyncio.to_thread(
                self.policy.analyze_frame,
                frame.image_data_url,
                frame.depth_hint,
            )

        @self.server.app.post("/speak")
        async def speak(payload: dict[str, Any]) -> Response:
            text = str(payload.get("text") or "").strip()
            if not text:
                return JSONResponse({"error": "Missing text"}, status_code=400)
            if len(text) > 240:
                return JSONResponse({"error": "Text is too long"}, status_code=400)

            speech = self._openai_client.audio.speech.create(
                model=self.tts_model,
                voice=str(payload.get("voice") or self.tts_voice),
                input=text,
                response_format="mp3",
            )
            return Response(content=speech.content, media_type="audio/mpeg")

        if STATIC_DIR.is_dir():
            self.server.app.mount(
                "/fetch/static",
                StaticFiles(directory=str(STATIC_DIR)),
                name="fetch_static",
            )

        @self.server.app.websocket("/fetch/ws")
        async def websocket_endpoint(ws: WebSocket) -> None:
            await ws.accept()
            await ws.send_json(
                {
                    "type": "hello",
                    "service": "fetch-iphone",
                    "model": self.policy.config.model,
                }
            )
            logger.info("Fetch iPhone client connected")
            try:
                while True:
                    message = await ws.receive_json()
                    message_type = message.get("type")
                    if message_type == "record3d_frame":
                        if self._record3d_source is None:
                            await ws.send_json(
                                {"type": "error", "message": "Record3D is not enabled"}
                            )
                            continue
                        frame = self._record3d_source.latest()
                        if frame is None:
                            await ws.send_json(
                                {"type": "error", "message": "No Record3D frame received yet"}
                            )
                            continue
                        decision = await asyncio.to_thread(
                            self.policy.analyze_frame,
                            frame.image_data_url,
                            frame.depth_hint,
                        )
                        decision["frame_id"] = message.get("frame_id")
                        decision["record3d"] = self._record3d_source.status()
                        await ws.send_json(decision)
                        continue

                    if message_type != "frame":
                        await ws.send_json(
                            {"type": "error", "message": "Expected frame or record3d_frame message"}
                        )
                        continue

                    image_data_url = str(message.get("image") or "")
                    depth_hint = message.get("depth_hint")
                    decision = await asyncio.to_thread(
                        self.policy.analyze_frame,
                        image_data_url,
                        depth_hint if isinstance(depth_hint, dict) else None,
                    )
                    decision["frame_id"] = message.get("frame_id")
                    await ws.send_json(decision)
            except WebSocketDisconnect:
                logger.info("Fetch iPhone client disconnected")
            except Exception as exc:
                logger.exception("Fetch WebSocket error")
                try:
                    await ws.send_json({"type": "error", "message": str(exc)})
                except Exception:
                    pass

    def run(self, ssl: bool = True) -> None:
        if self._record3d_source is not None:
            self._record3d_source.start()
        if ssl:
            certs_dir = get_project_root() / "assets" / "teleop_certs"
            self.server.run(ssl=True, ssl_certs_dir=certs_dir)
        else:
            self.server.run()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Fetch iPhone middleware.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host for the HTTPS server.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port.")
    parser.add_argument("--model", default="gpt-5-mini", help="OpenAI vision model.")
    parser.add_argument("--tts-model", default="tts-1", help="OpenAI TTS model.")
    parser.add_argument("--tts-voice", default="echo", help="OpenAI TTS voice.")
    parser.add_argument("--record3d", action="store_true", help="Read RGBD frames from Record3D over USB.")
    parser.add_argument("--record3d-device-index", type=int, default=0, help="Record3D device index.")
    parser.add_argument("--no-ssl", action="store_true", help="Disable HTTPS for local debugging.")
    return parser.parse_args()


def main() -> None:
    load_dotenv(get_project_root() / ".env")
    load_dotenv()
    args = _parse_args()
    middleware = FetchIphoneMiddleware(
        host=args.host,
        port=args.port,
        model=args.model,
        tts_model=args.tts_model,
        tts_voice=args.tts_voice,
        record3d=args.record3d,
        record3d_device_index=args.record3d_device_index,
    )
    scheme = "http" if args.no_ssl else "https"
    logger.info(f"Fetch iPhone middleware running at {scheme}://{args.host}:{args.port}/fetch")
    middleware.run(ssl=not args.no_ssl)


if __name__ == "__main__":
    main()
