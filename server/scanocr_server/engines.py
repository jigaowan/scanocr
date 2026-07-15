from __future__ import annotations

import json
import platform
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


class EngineError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EngineDescriptor:
    id: str
    kind: str
    display_name: str
    implementation_version: str
    model_name: str
    available: bool
    unavailable_reason: str
    supported_platforms: list
    supported_languages: Any
    capabilities: Dict[str, Any]
    max_concurrency: int

    def as_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


class NativeHelper:
    def __init__(self, path: Path):
        self.path = path

    def call(self, request: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
        if not self.path.is_file():
            raise EngineError("engine_unavailable", "native helper is not built: %s" % self.path)
        try:
            process = subprocess.run(
                [str(self.path)],
                input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise EngineError("timeout", "native helper timed out")
        if process.returncode != 0:
            message = process.stderr.decode("utf-8", "replace").strip() or "native helper failed"
            raise EngineError("execution_failed", message[-1000:])
        try:
            response = json.loads(process.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EngineError("execution_failed", "native helper returned invalid JSON")
        if not response.get("ok", False):
            raise EngineError(str(response.get("error_code", "execution_failed")), str(response.get("error_message", "engine failed")))
        return response


class EngineRegistry:
    def __init__(self, helper_path: Path):
        self.helper = NativeHelper(helper_path)
        available = platform.system() == "Darwin" and helper_path.is_file()
        reason = "" if available else "macOS native helper is not built"
        vision_languages: Any = "dynamic"
        translation_languages: Any = "dynamic"
        translation_pairs: Any = "dynamic"
        if available:
            try:
                capabilities = self.helper.call({"operation": "capabilities"}, timeout=30)
                vision_languages = capabilities.get("vision_languages", "dynamic")
                translation_languages = capabilities.get("translation_languages", "dynamic")
                translation_pairs = capabilities.get("translation_pairs", "dynamic")
            except EngineError as error:
                available = False
                reason = str(error)
        os_version = platform.mac_ver()[0]
        self.engines = {
            "vision": EngineDescriptor(
                id="vision", kind="ocr", display_name="Apple Vision", implementation_version="1",
                model_name="Vision / macOS %s" % os_version, available=available,
                unavailable_reason=reason, supported_platforms=["macos"], supported_languages=vision_languages,
                capabilities={"automatic_language_detection": True, "text_regions": True}, max_concurrency=1,
            ),
            "apple-translation": EngineDescriptor(
                id="apple-translation", kind="translation", display_name="Apple Translation",
                implementation_version="1", model_name="Translation Framework / macOS %s" % os_version,
                available=available, unavailable_reason=reason, supported_platforms=["macos"],
                supported_languages=translation_languages,
                capabilities={"automatic_language_detection": True, "language_pairs": translation_pairs,
                              "model_status_is_live": True}, max_concurrency=1,
            ),
        }
        for descriptor in self.engines.values():
            if descriptor.max_concurrency < 1:
                raise ValueError("engine max_concurrency must be positive")
        self.semaphores = {key: threading.BoundedSemaphore(value.max_concurrency) for key, value in self.engines.items()}

    def descriptor(self, engine_id: str, kind: str) -> EngineDescriptor:
        descriptor = self.engines.get(engine_id)
        if descriptor is None or descriptor.kind != kind:
            raise EngineError("engine_unavailable", "unknown %s engine: %s" % (kind, engine_id))
        if not descriptor.available:
            raise EngineError("engine_unavailable", descriptor.unavailable_reason)
        return descriptor

    def recognize(self, engine_id: str, image_path: Path, source_language: str) -> Dict[str, Any]:
        self.descriptor(engine_id, "ocr")
        with self.semaphores[engine_id]:
            return self.helper.call({
                "operation": "ocr", "image_path": str(image_path), "source_language": source_language,
            })

    def translate(self, engine_id: str, text: str, source_language: str, target_language: str) -> Dict[str, Any]:
        self.descriptor(engine_id, "translation")
        with self.semaphores[engine_id]:
            return self.helper.call({
                "operation": "translate", "text": text, "source_language": source_language,
                "target_language": target_language,
            })
