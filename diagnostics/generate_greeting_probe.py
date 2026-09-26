import base64
import pathlib
import time

import httpx
import ormsgpack

ref = pathlib.Path(
    r"E:\Mouth_Care_Solutions_Final_Default_Uploaded_Voice\mouth-care-solutions\fish-references\mouth-care-receptionist-reference-20260919.wav"
)

reference_text = "Tomorrow is holiday because of Sunday. The Sunday is because of today is Saturday. Today is Saturday is because of yesterday is Friday. Friday is because of Thursday. But I know you are not willing to listen, but you have to listen."
request_text = "Thank you for calling Mouth Care Solutions. How may I help you today?"

print("Reference:", ref)
print("Reference exists:", ref.exists())

if not ref.exists():
    raise SystemExit("REFERENCE AUDIO NOT FOUND")

payload = {
    "text": request_text,
    "format": "wav",
    "streaming": False,
    "normalize": True,
    "chunk_length": 200,
    "max_new_tokens": 400,
    "references": [
        {
            "audio": base64.b64encode(ref.read_bytes()).decode("ascii"),
            "text": reference_text,
        }
    ],
}

print("Sending request to Fish Speech...")
start = time.time()

response = httpx.post(
    "http://127.0.0.1:8080/v1/tts",
    content=ormsgpack.packb(payload, option=ormsgpack.OPT_SERIALIZE_PYDANTIC),
    headers={"content-type": "application/msgpack"},
    timeout=900.0,
)

elapsed = time.time() - start

print("HTTP:", response.status_code)
print("Content-Type:", response.headers.get("content-type"))
print("Bytes:", len(response.content))
print("Elapsed:", round(elapsed, 2), "seconds")

if response.status_code != 200:
    print(response.text[:2000])
    raise SystemExit(1)

out = pathlib.Path(
    r"E:\Mouth_Care_Solutions_Final_Default_Uploaded_Voice\mouth-care-solutions\diagnostics\fish_greeting_raw.wav"
)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(response.content)

print("Saved:", out)
print("Size:", out.stat().st_size)
