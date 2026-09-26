import io
import json
import time
import wave
from pathlib import Path

import ormsgpack
import requests

root = Path(__file__).resolve().parents[1]
reference = root / "fish-references" / "mouth-care-receptionist-reference-20260919.wav"
payload = {
    "text": "Thank you for calling Mouth Care Solutions. How may I help you today?",
    "format": "wav",
    "streaming": False,
    "normalize": True,
    "chunk_length": 200,
    "max_new_tokens": 400,
    "use_memory_cache": "on",
    "references": [{
        "audio": reference.read_bytes(),
        "text": "Tomorrow is holiday because of Sunday. The Sunday is because of today is Saturday. Today is Saturday is because of yesterday is Friday. Friday is because of Thursday. But I know you are not willing to listen, but you have to listen.",
    }],
}
started = time.perf_counter()
response = requests.post(
    "http://127.0.0.1:8080/v1/tts",
    data=ormsgpack.packb(payload, option=ormsgpack.OPT_SERIALIZE_PYDANTIC),
    headers={"Content-Type": "application/msgpack"},
    timeout=900,
)
result = {
    "status": response.status_code,
    "bytes": len(response.content),
    "elapsed_seconds": round(time.perf_counter() - started, 3),
    "content_type": response.headers.get("content-type", ""),
}
with wave.open(io.BytesIO(response.content), "rb") as wav_file:
    result.update({
        "wav_valid": True,
        "sample_rate": wav_file.getframerate(),
        "channels": wav_file.getnchannels(),
        "sample_width": wav_file.getsampwidth(),
        "frames": wav_file.getnframes(),
    })
print(json.dumps(result, indent=2))
(root / "diagnostics" / "fish_readiness_probe_20260923.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
