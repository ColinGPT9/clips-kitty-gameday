"""Rule 1 of the event: every model the app calls at runtime is one of Meta's,
and no other model or AI API is anywhere in the repository.

These fail when:
- requirements*.txt, pyproject.toml or a package.json lists a non-Meta ML or AI
  package, or an install command anywhere installs one;
- a Python or JavaScript file under gameday/, scripts/ or tests/ imports one;
- any file names a non-Meta model host (meta-relay/ is checked for hosts only);
- code under gameday/ or scripts/ names a non-Meta model;
- gameday.types.MODELS is anything but Meta's three models.
"""

from __future__ import annotations

import ast
import json
import os
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from gameday import types

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
             ".wrangler", "data", "build", "dist"}
MAX_BYTES = 2_000_000

META_MODELS = ("muse-voice-transcribe-1.0", "muse-spark-1.3", "sam-3.1")

# Python package names as PEP 503 writes them (lowercase, "-" for runs of
# "-", "_" and "."). A trailing "*" matches every name that starts with it.
BANNED_PACKAGES = (
    # ML frameworks and runtimes
    "torch*", "pytorch*", "tensorflow*", "tf-keras", "keras*", "jax", "jaxlib", "flax", "mxnet*", "paddlepaddle*",
    "onnx*", "openvino*", "tensorrt*", "tflite-runtime", "mlx*", "coremltools", "llama-cpp-python", "vllm",
    "gpt4all", "ctranslate2",
    # vision and OCR
    "opencv*", "ultralytics*", "yolov5", "mediapipe*", "insightface*", "dlib*", "face-recognition*", "deepface",
    "facenet-pytorch", "detectron2", "mmdet", "mmcv", "timm", "roboflow*", "inference-sdk",
    "rapidocr*", "paddleocr", "paddlenlp", "easyocr", "pytesseract", "tesserocr", "python-doctr",
    # speech and audio models
    "whisper*", "openai-whisper", "faster-whisper", "pywhispercpp", "vosk", "speechrecognition", "silero*",
    "panns*", "pyannote*", "speechbrain", "nemo-toolkit", "webrtcvad*",
    # model hubs and classic ML
    "transformers", "sentence-transformers", "ctransformers", "optimum*", "diffusers", "accelerate",
    "huggingface-hub", "tokenizers", "peft", "bitsandbytes", "scikit-learn", "sklearn", "xgboost", "lightgbm",
    "catboost", "spacy*", "nltk", "gensim",
    # AI APIs and frameworks over them
    "ollama*", "openai*", "anthropic*", "google-generativeai", "google-genai", "google-cloud-aiplatform",
    "vertexai", "google-cloud-speech", "google-cloud-vision", "mistralai", "mistral-*", "cohere*", "groq*",
    "litellm*", "langchain*", "langgraph*", "llama-index*", "llamaindex", "replicate", "together", "fireworks-ai",
    "deepgram-sdk", "assemblyai", "elevenlabs", "voyageai", "ai21", "instructor", "dspy*", "haystack-ai",
    "farm-haystack", "semantic-kernel", "autogen*", "pyautogen", "crewai*", "smolagents", "guidance", "outlines",
    "openrouter*",
)

# Python modules, as imported. A trailing "*" matches every top-level name
# that starts with it; a dotted entry matches it and what is under it.
BANNED_MODULES = (
    "torch*", "tensorflow*", "tf_keras", "keras*", "jax", "jaxlib", "flax", "mxnet", "paddle", "onnx*", "openvino",
    "tensorrt", "tflite_runtime", "mlx", "coremltools", "llama_cpp", "vllm", "gpt4all", "ctranslate2",
    "cv2", "ultralytics", "yolov5", "mediapipe", "insightface", "dlib", "face_recognition", "deepface",
    "facenet_pytorch", "detectron2", "mmdet", "mmcv", "timm", "roboflow", "inference_sdk",
    "rapidocr*", "paddleocr", "paddlenlp", "easyocr", "pytesseract", "tesserocr", "doctr",
    "whisper*", "faster_whisper", "pywhispercpp", "vosk", "speech_recognition", "silero*", "panns*", "pyannote",
    "speechbrain", "nemo", "webrtcvad",
    "transformers", "sentence_transformers", "ctransformers", "optimum", "diffusers", "accelerate", "huggingface_hub",
    "tokenizers", "peft", "bitsandbytes", "sklearn", "xgboost", "lightgbm", "catboost", "spacy", "nltk", "gensim",
    "ollama", "openai", "anthropic", "google.generativeai", "google.genai", "google.cloud.aiplatform", "vertexai",
    "google.cloud.speech", "google.cloud.vision", "mistralai", "cohere", "groq", "litellm", "langchain*",
    "langgraph*", "llama_index*", "replicate", "together", "fireworks", "deepgram", "assemblyai", "elevenlabs",
    "voyageai", "ai21", "instructor", "dspy", "haystack", "semantic_kernel", "autogen*", "crewai", "smolagents",
    "guidance", "outlines",
)

# npm packages. A trailing "*" matches every name that starts with it.
BANNED_NPM = (
    "openai", "@openai/*", "@anthropic-ai/*", "@google/generative-ai", "@google/genai", "@google-cloud/aiplatform",
    "@google-cloud/vertexai", "@mistralai/*", "cohere-ai", "groq-sdk", "langchain", "@langchain/*", "llamaindex",
    "@llamaindex/*", "ollama", "replicate", "together-ai", "@huggingface/*", "@xenova/transformers",
    "onnxruntime-*", "@tensorflow/*", "@tensorflow-models/*", "@mediapipe/*", "tesseract.js", "ai", "@ai-sdk/*",
    "@openrouter/*", "openrouter*", "@aws-sdk/client-bedrock*", "@azure/openai", "@fal-ai/*", "elevenlabs",
    "@deepgram/sdk", "assemblyai", "face-api.js", "ml5", "brain.js", "@gradio/client",
)

# Hosts of models that are not Meta's. Matched as host names, not as words.
NON_META_HOSTS = re.compile(
    r"(?:"
    r"api\.openai\.com|[\w-]+\.openai\.azure\.com|chatgpt\.com/backend-api|openrouter\.ai"
    r"|generativelanguage\.googleapis\.com|aiplatform\.googleapis\.com|speech\.googleapis\.com"
    r"|vision\.googleapis\.com|api\.anthropic\.com"
    r"|(?:api-inference|inference|router)\.huggingface\.co|[\w-]+\.endpoints\.huggingface\.cloud"
    r"|(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|host\.docker\.internal):(?:11434|1234)\b|ollama\.(?:com|ai)"
    r"|api\.groq\.com|api\.together\.(?:xyz|ai)|api\.mistral\.ai|api\.cohere\.(?:ai|com)|[\w.-]*nebius\.(?:ai|com|cloud)"
    r"|api\.deepseek\.com|(?:api\.)?replicate\.(?:com|delivery)|api\.fireworks\.ai|api\.perplexity\.ai|api\.x\.ai"
    r"|api\.cerebras\.ai|api\.sambanova\.ai|api\.deepinfra\.com|api\.novita\.ai|api\.hyperbolic\.xyz|api\.ai21\.com"
    r"|api\.voyageai\.com|api\.deepgram\.com|api\.assemblyai\.com|api\.elevenlabs\.io|api\.stability\.ai"
    r"|api\.runpod\.ai|bedrock-runtime\.[\w.-]*amazonaws\.com|dashscope\.aliyuncs\.com|api\.moonshot\.(?:cn|ai)"
    r"|open\.bigmodel\.cn|api\.siliconflow\.(?:cn|com)|(?:detect|serverless|api)\.roboflow\.com"
    r")(?![\w-])",
    re.IGNORECASE,
)

# Models that are not Meta's, as code would name them.
NON_META_MODEL_NAMES = re.compile(
    r"\b(?:gpt-?[345o][\w.-]*|chatgpt|o[134]-(?:mini|preview|pro)|claude-[\w.-]+|gemini-[\w.-]+"
    r"|gemma[\d:.-][\w.:-]*|mistral-[\w.-]+|mixtral|codestral|qwen[\w.:-]*|deepseek-[\w.-]+|phi-?[234][\w.-]*"
    r"|command-r[\w+-]*|whisper-(?:large|medium|small|base|tiny)[\w.-]*|yolo(?:v?\d+[\w-]*|-?nas|-world)?"
    r"|dall-e[\w-]*|grok-\d[\w.-]*|nemotron[\w.-]*|llava[\w.:-]*|moondream[\w.-]*|florence-2[\w.-]*"
    r"|paligemma[\w.-]*|stable-diffusion[\w.-]*)\b",
    re.IGNORECASE,
)

REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
INSTALL = re.compile(r"\b(?:pip3?\s+install|pipx\s+install|poetry\s+add|uv\s+add)\b(?P<args>[^\n;&|`#]*)")
NPM_INSTALL = re.compile(r"\b(?:npm\s+(?:install|i|add)|yarn\s+add|pnpm\s+(?:add|install))\b(?P<args>[^\n;&|`#]*)")
OPTIONS_WITH_VALUE = {"-r", "--requirement", "-c", "--constraint", "-e", "--editable", "-i", "--index-url",
                      "--extra-index-url", "-f", "--find-links", "-t", "--target", "--prefix", "--root",
                      "--python", "--registry", "--cache"}
JS_IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)["']([^"']+)["']""")


# ---- the files ---------------------------------------------------------------------------


def repo_files() -> list[Path]:
    out: list[Path] = []
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        out += [Path(folder, name) for name in sorted(files)]
    return out


def text_of(path: Path) -> str | None:
    """The file's text, or None when it is binary or too big to be source."""
    data = path.read_bytes()
    if len(data) > MAX_BYTES or b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def text_files() -> list[tuple[Path, str]]:
    out = []
    for path in repo_files():
        if path.resolve() == SELF:
            continue  # it names every banned thing, on purpose
        text = text_of(path)
        if text is not None:
            out.append((path, text))
    return out


def under(path: Path, *folders: str) -> bool:
    return rel(path).split("/", 1)[0] in folders


# ---- matching names ----------------------------------------------------------------------


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _hits(name: str, banned: tuple[str, ...]) -> str | None:
    for entry in banned:
        if name == entry or (entry.endswith("*") and name.startswith(entry[:-1])):
            return entry
    return None


def banned_package(name: str) -> str | None:
    return _hits(canonical(name), BANNED_PACKAGES)


def banned_npm(name: str) -> str | None:
    return _hits(name.lower(), BANNED_NPM)


def banned_module(name: str) -> str | None:
    top = name.split(".")[0]
    for entry in BANNED_MODULES:
        if entry.endswith("*"):
            if top.startswith(entry[:-1]):
                return entry
        elif name == entry or name.startswith(entry + "."):
            return entry
    return None


def requirement_names(specs) -> list[str]:
    names = []
    for spec in specs:
        spec = str(spec).split("#", 1)[0].strip()
        if not spec or spec.startswith("-"):
            continue
        found = REQUIREMENT_NAME.match(spec)
        if found:
            names.append(found.group(1))
    return names


def install_arguments(args: str) -> list[str]:
    """The packages an install command names: not its options, files or URLs."""
    names, skip_next = [], False
    for token in args.split():
        token = token.strip("'\"")
        if skip_next:
            skip_next = False
        elif token in OPTIONS_WITH_VALUE:
            skip_next = True
        elif token and not token.startswith(("-", ".", "/", "~")) and "://" not in token:
            names.append(token)
    return names


def npm_name(spec: str) -> str:
    parts = spec.split("/")
    return "/".join(parts[:2]) if spec.startswith("@") else parts[0]


def python_imports(source: str) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if called in ("__import__", "import_module") and isinstance(node.args[0].value, str):
                names.append(node.args[0].value)
    return names


# ---- the tests ---------------------------------------------------------------------------


def test_the_app_calls_only_metas_three_models():
    assert types.MODELS == META_MODELS
    assert (types.MODEL_VOICE, types.MODEL_SPARK, types.MODEL_SAM) == META_MODELS
    assert urlsplit(types.META_API_BASE).scheme == "https"
    assert urlsplit(types.META_API_BASE).hostname == "api.meta.ai"


def test_no_dependency_is_a_non_meta_ai_package():
    found = []
    for path in repo_files():
        name = path.name
        if re.fullmatch(r"requirements.*\.txt", name):
            specs = path.read_text(encoding="utf-8").splitlines()
        elif name == "pyproject.toml":
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            project = data.get("project", {})
            specs = list(project.get("dependencies", []))
            for group in project.get("optional-dependencies", {}).values():
                specs += group
            specs += data.get("build-system", {}).get("requires", [])
            for group in data.get("dependency-groups", {}).values():
                specs += [s for s in group if isinstance(s, str)]
        elif name == "package.json":
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
                found += [f"{rel(path)}: {n}" for n in (data.get(key) or {}) if banned_npm(n)]
            continue
        else:
            continue
        found += [f"{rel(path)}: {n}" for n in requirement_names(specs) if banned_package(n)]
    assert not found, "Non-Meta ML or AI packages listed:\n" + "\n".join(found)


def test_nothing_installs_a_non_meta_ai_package():
    found = []
    for path, text in text_files():
        joined = text.replace("\\\r\n", " ").replace("\\\n", " ")  # shell and Dockerfile line continuations
        for m in INSTALL.finditer(joined):
            for name in requirement_names(install_arguments(m["args"])):
                if banned_package(name):
                    found.append(f"{rel(path)}: {name}")
        for m in NPM_INSTALL.finditer(joined):
            for spec in install_arguments(m["args"]):
                name = spec.rsplit("@", 1)[0] if spec.rfind("@") > 0 else spec  # openai@4 -> openai
                if banned_npm(name):
                    found.append(f"{rel(path)}: {spec}")
    assert not found, "Install commands for non-Meta ML or AI packages:\n" + "\n".join(found)


def test_no_code_imports_a_non_meta_ai_package():
    found = []
    for path, text in text_files():
        if not under(path, "gameday", "scripts", "tests"):
            continue
        if path.suffix == ".py":
            found += [f"{rel(path)}: {n}" for n in python_imports(text) if banned_module(n)]
        elif path.suffix in (".js", ".mjs", ".cjs", ".ts"):
            for spec in JS_IMPORT.findall(text):
                if not spec.startswith((".", "/", "node:", "http:", "https:")) and banned_npm(npm_name(spec)):
                    found.append(f"{rel(path)}: {spec}")
    assert not found, "Imports of non-Meta ML or AI packages:\n" + "\n".join(found)


def test_no_file_names_a_non_meta_model_host():
    found = []
    for path, text in text_files():
        for number, line in enumerate(text.splitlines(), start=1):
            found += [f"{rel(path)}:{number}: {m.group(0)}" for m in NON_META_HOSTS.finditer(line)]
    assert not found, "Non-Meta model hosts named:\n" + "\n".join(found)


def test_no_code_names_a_non_meta_model():
    found = []
    for path, text in text_files():
        if not under(path, "gameday", "scripts") or path.suffix not in (".py", ".js", ".mjs", ".ts", ".html"):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            found += [f"{rel(path)}:{number}: {m.group(0)}" for m in NON_META_MODEL_NAMES.finditer(line)]
    assert not found, "Non-Meta models named in code:\n" + "\n".join(found)


def test_the_checks_catch_what_they_are_for():
    """The lists above match what they should, and not Meta's own package or plain words."""
    assert banned_package("opencv-python-headless") and banned_package("Faster_Whisper")
    assert banned_package("langchain-openai") and banned_package("google.generativeai")
    assert not banned_package("meta-sam-parser") and not banned_package("numpy")
    assert banned_module("cv2") and banned_module("google.genai.types") and banned_module("langchain_core.llms")
    assert not banned_module("gameday.engine") and not banned_module("google.protobuf")
    assert banned_npm("@anthropic-ai/sdk") and banned_npm("onnxruntime-node") and not banned_npm("aio")
    assert NON_META_HOSTS.search("POST https://api.openai.com/v1/chat") and NON_META_HOSTS.search("localhost:11434")
    assert NON_META_HOSTS.search("https://openrouter.ai/api/v1") and NON_META_HOSTS.search("api.replicate.com")
    assert not NON_META_HOSTS.search("We replicate the clip. Combined with the rest") and not NON_META_HOSTS.search(
        "https://api.meta.ai/v1")
    assert NON_META_MODEL_NAMES.search('model="gpt-4o-mini"') and not NON_META_MODEL_NAMES.search("muse-spark-1.3")
    assert install_arguments(" -r requirements.txt --quiet torch==2.4") == ["torch==2.4"]
    assert python_imports("import importlib\nimportlib.import_module('openai')\n") == ["importlib", "openai"]
