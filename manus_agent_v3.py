#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TerraChat Agent v3 — Arquitectura Plan-and-Execute (inspirada en Manus)

Cambios fundamentales respecto a v2:
1. PLAN-AND-EXECUTE: El agente primero crea un plan (todo.md), luego ejecuta paso a paso.
2. FILESYSTEM-AS-MEMORY: El workspace local es la memoria primaria. No hay servidor externo.
3. TODO.MD TRACKING: Cada tarea tiene estado [ ] / [x] / [-] (pendiente/hecho/fallido).
4. CONTEXT COMPACTION: El historial del LLM se comprime automáticamente para evitar overflow.
5. TOOL-FIRST: Cada ciclo del agente DEBE usar al menos una herramienta antes de responder.
6. RELAY INTEGRATION: Conexión WebSocket al relay para observabilidad y mensajería.
7. PERSISTENT WORKSPACE: Los archivos persisten entre reinicios del agente.
"""

import os
import sys
import json
import time
import asyncio
import re
import subprocess
import ssl
import traceback
import tempfile
import random
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from openai import OpenAI
try:
    import websockets
except ImportError:
    import subprocess as _sp
    _sp.run(["pip", "install", "websockets", "-q"], check=True)
    import websockets

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════════════

AGENT_ID       = os.environ.get("AGENT_ID", "manus_agent_dev")
DISPLAY_NAME   = os.environ.get("DISPLAY_NAME", "DEV")
SOUL_PROMPT    = os.environ.get("SOUL_PROMPT", "Eres un agente de IA autónomo.")
RELAY_URL      = os.environ.get("RELAY_URL", "wss://web-production-76b83.up.railway.app/ws")
MEMORY_URL     = os.environ.get("MEMORY_URL", "https://memory-server-production-647a.up.railway.app")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

MODEL_PLAN     = "gemini-2.5-flash"   # Para planificación y análisis
MODEL_EXECUTE  = "gpt-4.1-mini"       # Para ejecución de pasos y chat
MODEL_FAST     = "gpt-4.1-nano"       # Para clasificación rápida

# Directorios de trabajo (persisten en el sandbox E2B)
WORKSPACE_DIR  = Path("/home/ubuntu/workspace")
DOCS_DIR       = WORKSPACE_DIR / "docs"
MEMORY_DIR     = Path("/home/ubuntu/memory")

# Archivos de estado
TODO_FILE      = WORKSPACE_DIR / "todo.md"
STATUS_FILE    = MEMORY_DIR / "status.json"
ROOM_FILE      = MEMORY_DIR / "active_room.txt"
LOG_FILE       = MEMORY_DIR / "agent.log"

# Límites
MAX_HISTORY    = 30    # Máximo de mensajes en historial antes de comprimir
MAX_MSG_CHARS  = 600   # Máximo de caracteres en un mensaje visible al chat
MAX_FILE_READ  = 8000  # Máximo de caracteres al leer un archivo

# ══════════════════════════════════════════════════════════════════════════════
# INICIALIZACIÓN
# ══════════════════════════════════════════════════════════════════════════════

def init_dirs():
    for d in [WORKSPACE_DIR, DOCS_DIR, MEMORY_DIR]:
        d.mkdir(parents=True, exist_ok=True)

client_llm = OpenAI()

# Historial de conversación (en memoria, se comprime automáticamente)
history: list[dict] = []

# Log de actividad (últimas 30 líneas, para el panel de workspace)
_activity_log: list[str] = []

def log(msg: str):
    """Registra un mensaje en el log de actividad y en el archivo de log."""
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    print(entry, flush=True)
    _activity_log.append(entry)
    if len(_activity_log) > 30:
        _activity_log.pop(0)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(entry + "\n")
    except Exception:
        pass

def update_status(state: str, extra: dict = None):
    data = {
        "agent": AGENT_ID,
        "display": DISPLAY_NAME,
        "state": state,
        "ts": datetime.now().isoformat(),
        "log": "\n".join(_activity_log[-20:]),
        **(extra or {})
    }
    try:
        STATUS_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════════════
# CAPA 1: TODO.MD — GESTIÓN DE TAREAS
# ══════════════════════════════════════════════════════════════════════════════

def todo_read() -> str:
    """Lee el archivo todo.md completo."""
    if not TODO_FILE.exists():
        return ""
    return TODO_FILE.read_text(encoding="utf-8")

def todo_get_next() -> str | None:
    """Obtiene la siguiente tarea pendiente [ ]."""
    content = todo_read()
    m = re.search(r"- \[ \] (.+)", content)
    return m.group(1).strip() if m else None

def todo_mark_done(task: str):
    """Marca una tarea como completada [x]."""
    content = todo_read()
    new_content = content.replace(f"- [ ] {task}", f"- [x] {task}", 1)
    TODO_FILE.write_text(new_content, encoding="utf-8")

def todo_mark_failed(task: str):
    """Marca una tarea como fallida [-]."""
    content = todo_read()
    new_content = content.replace(f"- [ ] {task}", f"- [-] {task}", 1)
    TODO_FILE.write_text(new_content, encoding="utf-8")

def todo_add_task(task: str):
    """Añade una nueva tarea al final del todo.md."""
    content = todo_read()
    if not content:
        content = "# Plan de Tareas\n\n"
    content += f"- [ ] {task}\n"
    TODO_FILE.write_text(content, encoding="utf-8")

def todo_create_plan(tasks: list[str]):
    """Crea un nuevo plan de tareas, reemplazando el actual."""
    lines = ["# Plan de Tareas\n"]
    for t in tasks:
        lines.append(f"- [ ] {t}")
    TODO_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

# ══════════════════════════════════════════════════════════════════════════════
# CAPA 2: HERRAMIENTAS
# ══════════════════════════════════════════════════════════════════════════════

def tool_search(query: str) -> str:
    """Búsqueda web via Tavily API (profundidad avanzada, 7 resultados)."""
    try:
        key = TAVILY_API_KEY
        if not key:
            return "Error: TAVILY_API_KEY no configurada."
        try:
            from tavily import TavilyClient
        except ImportError:
            subprocess.run([sys.executable, "-m", "pip", "install", "tavily-python", "-q"], check=True)
            from tavily import TavilyClient
        client = TavilyClient(api_key=key)
        resp = client.search(query=query, search_depth="advanced", max_results=7)
        results = []
        for r in resp.get("results", []):
            results.append(f"• {r['title']} — {r['url']}\n  {r.get('content','')[:300]}")
        return "\n".join(results) if results else "Sin resultados."
    except Exception as e:
        return f"Error en búsqueda: {e}"

def tool_browse(url: str) -> str:
    """Obtiene y parsea el contenido de texto de una URL (hasta 6000 chars)."""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; TerraBot/3.0)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="replace")
        # Eliminar scripts, estilos y tags HTML
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:6000]
    except Exception as e:
        return f"Error browse: {e}"

def tool_read(path: str) -> str:
    """Lee un archivo del workspace. Busca primero en docs/, luego en workspace/."""
    try:
        # Intentar rutas en orden de prioridad
        candidates = [
            Path(path),
            DOCS_DIR / path,
            WORKSPACE_DIR / path,
        ]
        for p in candidates:
            if p.exists():
                content = p.read_text(encoding="utf-8", errors="replace")
                return content[:MAX_FILE_READ] if len(content) > MAX_FILE_READ else content
        return f"Archivo no encontrado: {path}"
    except Exception as e:
        return f"Error leyendo: {e}"

def tool_write(path: str, content: str) -> str:
    """
    Escribe un archivo en el workspace Y lo sincroniza al Memory Server
    para que aparezca en el panel de Documentos del dashboard.
    """
    try:
        # Resolver ruta: si no es absoluta, guardar en docs/
        p = Path(path)
        if not p.is_absolute():
            p = DOCS_DIR / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        log(f"✏️  Escrito: {p.name} ({len(content):,} chars)")

        # Sincronizar al Memory Server con prefijo doc:
        if p.suffix in ('.md', '.txt', '.py', '.json', '.csv', '.html') and len(content) > 50:
            doc_key = f"doc:{p.stem}"
            _mem_set(doc_key, content)
            log(f"📄 DOC SYNC → Memory Server: {doc_key}")

        return f"Escrito: {p} ({len(content):,} chars)"
    except Exception as e:
        return f"Error escribiendo: {e}"

def tool_exec(code: str, timeout: int = 60) -> str:
    """Ejecuta código Python en un subprocess aislado con acceso al workspace."""
    try:
        code = code.replace('\\n', '\n').replace('\\t', '\t')
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, dir='/tmp') as f:
            f.write(code)
            tmp_path = f.name
        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(WORKSPACE_DIR),
            env={**os.environ, "WORKSPACE": str(WORKSPACE_DIR)}
        )
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        output = result.stdout
        if result.stderr:
            output += "\n[stderr]: " + result.stderr
        return output[:4000] if len(output) > 4000 else output or "(sin salida)"
    except subprocess.TimeoutExpired:
        return f"Error: timeout ({timeout}s)"
    except Exception as e:
        return f"Error exec: {e}"

def tool_bash(command: str, timeout: int = 30) -> str:
    """Ejecuta un comando bash en el workspace."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=str(WORKSPACE_DIR)
        )
        output = result.stdout + result.stderr
        return output[:3000] if len(output) > 3000 else output or "(sin salida)"
    except subprocess.TimeoutExpired:
        return f"Error: timeout ({timeout}s)"
    except Exception as e:
        return f"Error bash: {e}"

def tool_list_files() -> str:
    """Lista todos los archivos en el workspace."""
    try:
        files = []
        for p in sorted(WORKSPACE_DIR.rglob("*")):
            if p.is_file() and not p.name.startswith('.'):
                rel = p.relative_to(WORKSPACE_DIR)
                size = p.stat().st_size
                files.append(f"  {rel} ({size:,} bytes)")
        return "\n".join(files) if files else "(workspace vacío)"
    except Exception as e:
        return f"Error listando: {e}"

def tool_todo(action: str, task: str = "") -> str:
    """Gestiona el todo.md: add/done/fail/list."""
    if action == "list":
        return todo_read() or "(sin tareas)"
    elif action == "add":
        todo_add_task(task)
        return f"Tarea añadida: {task}"
    elif action == "done":
        todo_mark_done(task)
        return f"Tarea completada: {task}"
    elif action == "fail":
        todo_mark_failed(task)
        return f"Tarea marcada como fallida: {task}"
    return f"Acción desconocida: {action}"

# ══════════════════════════════════════════════════════════════════════════════
# CAPA 3: MEMORIA (Memory Server HTTP + filesystem)
# ══════════════════════════════════════════════════════════════════════════════

def _mem_set(key: str, value: str) -> bool:
    """Guarda un valor en el Memory Server via HTTP."""
    try:
        data = json.dumps({"key": key, "value": value, "agent": AGENT_ID}).encode()
        req = urllib.request.Request(
            f"{MEMORY_URL}/memory",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status < 300
    except Exception:
        return False

def _mem_get(key: str) -> str:
    """Lee un valor del Memory Server via HTTP."""
    try:
        url = f"{MEMORY_URL}/memory/{urllib.parse.quote(key, safe='')}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
            return data.get("value", "")
    except Exception:
        return ""

def _mem_get_all() -> dict:
    """Lee todas las entradas del Memory Server."""
    try:
        req = urllib.request.Request(f"{MEMORY_URL}/memory")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return {}

def tool_memory(action: str, key: str = "", value: str = "") -> str:
    """Gestiona la memoria compartida entre agentes."""
    if action == "set":
        ok = _mem_set(key, value)
        return f"Memoria guardada: {key}" if ok else f"Error guardando: {key}"
    elif action == "get":
        if key == "*":
            all_mem = _mem_get_all()
            if not all_mem:
                return "(memoria vacía)"
            lines = [f"• {k}: {v.get('value','')[:100]}" for k, v in list(all_mem.items())[-20:]]
            return "\n".join(lines)
        val = _mem_get(key)
        return val if val else f"(no encontrado: {key})"
    return f"Acción desconocida: {action}"

# ══════════════════════════════════════════════════════════════════════════════
# CAPA 4: LLM — PLANIFICACIÓN Y EJECUCIÓN
# ══════════════════════════════════════════════════════════════════════════════

TOOL_REFERENCE = """
HERRAMIENTAS DISPONIBLES:
  ##SEARCH: <consulta>##                          → búsqueda web (Tavily, 7 resultados)
  ##BROWSE: <url>##                               → leer contenido de URL (hasta 6000 chars)
  ##READ: <ruta>##                                → leer archivo del workspace
  ##WRITE: <ruta>\n<contenido>\n##END_WRITE##     → escribir archivo (se sincroniza al panel Docs)
  ##EXEC:\n<código python>\n##END_EXEC##          → ejecutar código Python
  ##BASH: <comando>##                             → ejecutar comando bash
  ##FILES##                                       → listar archivos en el workspace
  ##TODO: list##                                  → ver tareas pendientes
  ##TODO: add|<tarea>##                           → añadir tarea al plan
  ##TODO: done|<tarea>##                          → marcar tarea como completada
  ##MEMORY: set|<clave>|<valor>##                 → guardar en memoria colectiva
  ##MEMORY: get|<clave>##                         → leer memoria colectiva (usa * para todo)
  ##ROOM: <nombre>##                              → cambiar de sala de chat

REGLAS DE PRODUCCIÓN:
  1. PLAN PRIMERO: Si no tienes un plan claro, crea uno con ##TODO: add## antes de actuar.
  2. FILESYSTEM-AS-MEMORY: Guarda resultados en archivos con ##WRITE##, no solo en memoria.
  3. BROWSE DESPUÉS DE SEARCH: ##SEARCH## da titulares. Para datos reales, haz ##BROWSE## a las URLs.
  4. DOCUMENTOS SIEMPRE: Cada investigación, análisis o resultado → archivo .md con ##WRITE##.
  5. MENSAJES CORTOS: Máximo 3 frases en el chat. El contenido va en archivos, no en mensajes.
  6. NUNCA inventes datos — solo usa información real obtenida con herramientas.
  7. NUNCA menciones errores técnicos (404, timeout) en el chat.
"""

SYSTEM_PROMPT_BASE = f"""{SOUL_PROMPT}

Tienes acceso a un workspace local persistente en /home/ubuntu/workspace/ donde puedes leer y escribir archivos.
Tu memoria primaria es el filesystem: guarda resultados, investigaciones y planes como archivos .md.
Usas un todo.md para gestionar tu plan de trabajo.

{TOOL_REFERENCE}
"""

def llm_call_sync(messages: list, model: str = MODEL_EXECUTE, max_tokens: int = 2000, temperature: float = 0.7) -> str:
    """Llamada síncrona al LLM con reintentos en caso de rate limit."""
    for attempt in range(3):
        try:
            resp = client_llm.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            err = str(e)
            if any(x in err for x in ['429', 'rate_limit', '403', 'Forbidden']):
                wait = (attempt + 1) * 20
                log(f"⚠️  LLM rate-limit (intento {attempt+1}/3) — esperando {wait}s")
                time.sleep(wait)
                continue
            raise
    raise Exception("LLM: máximo de reintentos alcanzado")

async def llm_call(messages: list, model: str = MODEL_EXECUTE, max_tokens: int = 2000, temperature: float = 0.7) -> str:
    """Llamada asíncrona al LLM (ejecuta en thread pool para no bloquear el event loop)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, lambda: llm_call_sync(messages, model, max_tokens, temperature)
    )

async def create_plan(goal: str) -> list[str]:
    """Usa el LLM para crear un plan de tareas concretas para alcanzar un objetivo."""
    log(f"🧠 Creando plan para: {goal[:80]}")
    prompt = f"""Eres {DISPLAY_NAME}, un agente de IA autónomo.

Tu objetivo es: {goal}

Crea un plan de 3-7 tareas concretas y ejecutables para alcanzar este objetivo.
Cada tarea debe ser específica y producir un resultado tangible (un archivo, datos, código).
Las tareas deben estar en orden lógico de ejecución.

Responde SOLO con una lista JSON de strings. Ejemplo:
["Buscar información sobre X con Tavily", "Navegar a las 3 URLs más relevantes", "Escribir resumen en research.md", "Crear análisis final en report.md"]
"""
    response = await llm_call(
        [{"role": "user", "content": prompt}],
        model=MODEL_PLAN,
        max_tokens=500,
        temperature=0.3
    )
    try:
        m = re.search(r'\[.*\]', response, re.DOTALL)
        if m:
            tasks = json.loads(m.group(0))
            return [str(t) for t in tasks if t]
    except Exception:
        pass
    # Fallback: extraer líneas numeradas
    tasks = re.findall(r'\d+\.\s+(.+)', response)
    return tasks if tasks else [goal]

# ══════════════════════════════════════════════════════════════════════════════
# CAPA 5: PARSER DE COMANDOS
# ══════════════════════════════════════════════════════════════════════════════

def extract_commands(text: str) -> list[dict]:
    """Extrae todos los comandos ##...## del texto del LLM."""
    cmds = []

    # SEARCH
    for m in re.finditer(r'##SEARCH:\s*(.+?)##', text, re.IGNORECASE):
        cmds.append({"type": "search", "query": m.group(1).strip()})

    # BROWSE
    for m in re.finditer(r'##BROWSE:\s*(https?://\S+?)##', text, re.IGNORECASE):
        cmds.append({"type": "browse", "url": m.group(1).strip()})

    # READ
    for m in re.finditer(r'##READ:\s*(.+?)##', text, re.IGNORECASE):
        cmds.append({"type": "read", "path": m.group(1).strip()})

    # WRITE
    for m in re.finditer(r'##WRITE:\s*(\S+)\s*\n(.*?)##END_WRITE##', text, re.DOTALL | re.IGNORECASE):
        cmds.append({"type": "write", "path": m.group(1).strip(), "content": m.group(2)})

    # EXEC
    for m in re.finditer(r'##EXEC:\s*\n(.*?)##END_EXEC##', text, re.DOTALL | re.IGNORECASE):
        cmds.append({"type": "exec", "code": m.group(1)})

    # BASH
    for m in re.finditer(r'##BASH:\s*(.+?)##', text, re.IGNORECASE):
        cmds.append({"type": "bash", "command": m.group(1).strip()})

    # FILES
    for m in re.finditer(r'##FILES##', text, re.IGNORECASE):
        cmds.append({"type": "files"})

    # TODO
    for m in re.finditer(r'##TODO:\s*(list|add|done|fail)\|?(.+?)?##', text, re.IGNORECASE):
        cmds.append({"type": "todo", "action": m.group(1).lower(), "task": (m.group(2) or "").strip()})

    # MEMORY
    for m in re.finditer(r'##MEMORY:\s*(set|get)\|([^|#]+)(?:\|(.+?))?##', text, re.DOTALL | re.IGNORECASE):
        cmds.append({"type": "memory", "action": m.group(1).lower(), "key": m.group(2).strip(), "value": (m.group(3) or "").strip()})

    # ROOM
    for m in re.finditer(r'##ROOM:\s*([a-zA-Z0-9_\-]+)##', text, re.IGNORECASE):
        cmds.append({"type": "room", "name": m.group(1).strip().lower()})

    return cmds

async def execute_command(cmd: dict, send_fn) -> str:
    """Ejecuta un comando extraído del LLM."""
    t = cmd["type"]
    loop = asyncio.get_event_loop()
    log(f"🔧 Ejecutando: {t}")
    update_status("executing", {"tool": t})

    try:
        if t == "search":
            return await asyncio.wait_for(loop.run_in_executor(None, tool_search, cmd["query"]), timeout=30)
        elif t == "browse":
            return await asyncio.wait_for(loop.run_in_executor(None, tool_browse, cmd["url"]), timeout=25)
        elif t == "read":
            return await loop.run_in_executor(None, tool_read, cmd["path"])
        elif t == "write":
            return await loop.run_in_executor(None, tool_write, cmd["path"], cmd["content"])
        elif t == "exec":
            return await asyncio.wait_for(loop.run_in_executor(None, tool_exec, cmd["code"]), timeout=60)
        elif t == "bash":
            return await asyncio.wait_for(loop.run_in_executor(None, tool_bash, cmd["command"]), timeout=30)
        elif t == "files":
            return await loop.run_in_executor(None, tool_list_files)
        elif t == "todo":
            return await loop.run_in_executor(None, tool_todo, cmd["action"], cmd.get("task", ""))
        elif t == "memory":
            return await loop.run_in_executor(None, tool_memory, cmd["action"], cmd.get("key", ""), cmd.get("value", ""))
        elif t == "room":
            ROOM_FILE.write_text(cmd["name"])
            return f"Sala activa: #{cmd['name']}"
        else:
            return f"Comando desconocido: {t}"
    except asyncio.TimeoutError:
        return f"Error: herramienta '{t}' superó el tiempo límite"
    except Exception as e:
        return f"Error en {t}: {e}"

# ══════════════════════════════════════════════════════════════════════════════
# CAPA 6: BUCLE PRINCIPAL PLAN-AND-EXECUTE
# ══════════════════════════════════════════════════════════════════════════════

def clean_message(text: str) -> str:
    """Elimina todos los marcadores ##...## del texto visible."""
    text = re.sub(r'##EXEC:.*?##END_EXEC##', '', text, flags=re.DOTALL)
    text = re.sub(r'##WRITE:\S+\s*\n.*?##END_WRITE##', '', text, flags=re.DOTALL)
    text = re.sub(r'##[A-Z_]+:.*?##', '', text, flags=re.DOTALL)
    text = re.sub(r'##[A-Z_]+##', '', text)
    # Eliminar monólogo interno
    text = re.sub(r'^(Acabo de|He (compartido|enviado|buscado|creado)|Voy a|Procedo a|Buscaré|Ejecutaré).*$', '', text, flags=re.MULTILINE | re.IGNORECASE)
    return text.strip()

def get_active_room() -> str:
    try:
        if ROOM_FILE.exists():
            return ROOM_FILE.read_text().strip() or "general"
    except Exception:
        pass
    return "general"

def compress_history():
    """Comprime el historial cuando supera MAX_HISTORY entradas."""
    global history
    if len(history) <= MAX_HISTORY:
        return
    # Resumir los primeros 2/3 del historial
    cutoff = len(history) * 2 // 3
    to_compress = history[:cutoff]
    recent = history[cutoff:]

    # Crear resumen
    text_to_summarize = "\n".join([
        f"{m['role']}: {m['content'][:200]}" for m in to_compress
    ])
    try:
        summary = llm_call_sync(
            [{"role": "user", "content": f"Resume estos mensajes en 3-5 puntos clave:\n\n{text_to_summarize}"}],
            model=MODEL_FAST, max_tokens=400, temperature=0.3
        )
        history = [
            {"role": "user", "content": f"[CONTEXTO COMPRIMIDO]\n{summary}"},
            {"role": "assistant", "content": "Contexto anterior cargado."}
        ] + recent
        log(f"📦 Historial comprimido: {len(to_compress)} → 2 entradas")
    except Exception as e:
        log(f"⚠️  Error comprimiendo historial: {e}")
        history = recent  # Fallback: solo mantener los recientes

async def process_message(incoming: str, sender: str, send_fn, reply_room: str = None):
    """
    Procesa un mensaje entrante con el ciclo Plan-and-Execute:
    1. Añadir al historial
    2. Llamar al LLM con el sistema de herramientas
    3. Ejecutar comandos extraídos
    4. Hacer follow-up con el LLM sobre los resultados
    5. Enviar respuesta visible al chat
    """
    global history
    update_status("thinking")

    # Añadir al historial
    history.append({"role": "user", "content": f"[{sender}]: {incoming}"})
    compress_history()

    # Preparar contexto del workspace
    workspace_context = f"\nWorkspace actual:\n{tool_list_files()}\nTodo.md:\n{todo_read() or '(vacío)'}\n"

    system_msg = SYSTEM_PROMPT_BASE + workspace_context

    # Llamada al LLM
    log(f"🧠 LLM [{MODEL_EXECUTE}] procesando...")
    reply = await llm_call(
        [{"role": "system", "content": system_msg}, *history],
        model=MODEL_EXECUTE,
        max_tokens=2500
    )
    history.append({"role": "assistant", "content": reply})

    # Extraer y ejecutar comandos
    cmds = extract_commands(reply)
    room_cmds = [c for c in cmds if c["type"] == "room"]
    tool_cmds = [c for c in cmds if c["type"] != "room"]

    # Cambiar sala si hay room switch
    target_room = reply_room or get_active_room()
    for rc in room_cmds:
        ROOM_FILE.write_text(rc["name"])
        target_room = rc["name"]
        log(f"🔀 Sala: #{target_room}")

    # Enviar mensaje visible (antes de ejecutar herramientas)
    visible = clean_message(reply)
    if visible and len(visible) > MAX_MSG_CHARS:
        first_para = visible.split('\n\n')[0].strip()
        visible = first_para[:MAX_MSG_CHARS - 3] + '...' if len(first_para) > MAX_MSG_CHARS else first_para
    if visible:
        await send_fn(visible, target_room)
        log(f"💬 SENT #{target_room}: {visible[:80]}")

    # Ejecutar herramientas
    if tool_cmds:
        results = []
        for cmd in tool_cmds:
            result = await execute_command(cmd, send_fn)
            results.append(f"[{cmd['type']}]: {result}")
            log(f"  → {result[:120]}")

        combined = "\n\n".join(results)
        update_status("thinking")

        # Follow-up: el LLM analiza los resultados y decide qué hacer
        is_error = combined.strip()[:100].lower().startswith("error")
        if not is_error and len(combined.strip()) > 30:
            follow_prompt = (
                f"Resultados de las herramientas:\n{combined[:3000]}\n\n"
                "REGLA: Si guardaste un archivo, di solo su nombre en 1-2 frases. "
                "NO copies el contenido en el chat. "
                "Si necesitas más pasos, usa las herramientas directamente. "
                "Si terminaste la tarea, márcala como completada con ##TODO: done|<tarea>##."
            )
            history.append({"role": "user", "content": follow_prompt})
            follow_reply = await llm_call(
                [{"role": "system", "content": system_msg}, *history],
                model=MODEL_EXECUTE, max_tokens=1000
            )
            history.append({"role": "assistant", "content": follow_reply})

            # Ejecutar comandos del follow-up (especialmente ##TODO: done##)
            follow_cmds = extract_commands(follow_reply)
            for cmd in follow_cmds:
                await execute_command(cmd, send_fn)

            # Enviar mensaje visible del follow-up
            follow_visible = clean_message(follow_reply)
            if follow_visible and len(follow_visible) > MAX_MSG_CHARS:
                first_para = follow_visible.split('\n\n')[0].strip()
                follow_visible = first_para[:MAX_MSG_CHARS - 3] + '...' if len(first_para) > MAX_MSG_CHARS else first_para
            if follow_visible:
                await send_fn(follow_visible, target_room)
                log(f"💬 FOLLOW-UP #{target_room}: {follow_visible[:80]}")

    update_status("idle")

# ══════════════════════════════════════════════════════════════════════════════
# CAPA 7: PROACTIVIDAD Y AUTONOMÍA
# ══════════════════════════════════════════════════════════════════════════════

async def proactive_startup(send_fn):
    """Al conectarse, el agente revisa su estado y retoma el trabajo pendiente."""
    await asyncio.sleep(10)
    log("🚀 Iniciando acción proactiva de arranque...")

    # Revisar si hay tareas pendientes
    next_task = todo_get_next()
    if next_task:
        log(f"📋 Retomando tarea pendiente: {next_task}")
        await process_message(
            f"Retoma tu trabajo. Tu próxima tarea pendiente es: '{next_task}'. "
            "Ejecútala ahora usando las herramientas disponibles. "
            "Empieza directamente con ##SEARCH## o ##BROWSE## si necesitas información, "
            "o con ##WRITE## si ya tienes el contenido.",
            "sistema", send_fn
        )
    else:
        # No hay tareas — crear un plan basado en la misión
        log("📋 Sin tareas pendientes — creando plan inicial...")
        await process_message(
            f"Eres {DISPLAY_NAME}. No tienes tareas pendientes. "
            "Revisa la memoria colectiva con ##MEMORY: get|*## para ver el contexto del equipo. "
            "Luego crea un plan de trabajo con ##TODO: add## para las próximas 3-5 tareas "
            "que deberías ejecutar para cumplir tu misión. "
            "Empieza a ejecutar la primera tarea inmediatamente.",
            "sistema", send_fn
        )

async def silence_monitor(send_fn):
    """Activa trabajo autónomo cuando hay silencio prolongado."""
    last_active = [time.time()]
    cycle = [0]

    WORK_PROMPTS = [
        (
            "Han pasado 2 minutos sin actividad. "
            "Revisa tu todo.md con ##TODO: list## y ejecuta la siguiente tarea pendiente. "
            "Si no hay tareas, crea nuevas con ##TODO: add## basadas en tu misión. "
            "OBLIGATORIO: tu respuesta DEBE incluir al menos ##SEARCH##, ##BROWSE##, o ##WRITE##."
        ),
        (
            "Momento de avanzar en tu misión. "
            "Navega a una web relevante: ##BROWSE: <url>## "
            "o busca información: ##SEARCH: <consulta>## "
            "Luego escribe los hallazgos en un archivo .md con ##WRITE##."
        ),
        (
            "Revisa la memoria colectiva ##MEMORY: get|*## y el workspace ##FILES##. "
            "¿Qué trabajo está pendiente? ¿Qué puede mejorar? "
            "Elige la acción más valiosa y ejecútala ahora."
        ),
    ]

    while True:
        await asyncio.sleep(20)
        silence = time.time() - last_active[0]
        if silence > 120:  # 2 minutos de silencio
            prompt = WORK_PROMPTS[cycle[0] % len(WORK_PROMPTS)]
            cycle[0] += 1
            last_active[0] = time.time()
            await process_message(prompt, "sistema", send_fn)

# ══════════════════════════════════════════════════════════════════════════════
# CAPA 8: CONEXIÓN AL RELAY
# ══════════════════════════════════════════════════════════════════════════════

async def run():
    """Bucle principal de conexión al relay WebSocket."""
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    retries = 0
    update_status("connecting")

    # Cargar contexto de sesiones anteriores
    shared_context = tool_memory("get", "*")
    if shared_context and shared_context != "(memoria vacía)":
        history.append({"role": "user", "content": f"[CONTEXTO COLECTIVO]\n{shared_context[:1000]}"})
        history.append({"role": "assistant", "content": "Contexto del equipo cargado."})
        log(f"📚 Contexto colectivo cargado")

    while True:
        try:
            log(f"🔌 Conectando al relay... (intento {retries + 1})")
            async with websockets.connect(
                RELAY_URL, ssl=ssl_ctx, open_timeout=20, ping_interval=None
            ) as ws:
                retries = 0
                update_status("connected")
                log(f"✅ Conectado al relay")

                # Handshake
                await ws.send(json.dumps({
                    "protocol_version": "0.1",
                    "message_type": "HELLO",
                    "sender": AGENT_ID,
                    "display_name": DISPLAY_NAME,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }))
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
                welcome = json.loads(raw)
                log(f"👋 Bienvenida recibida | agentes online: {welcome.get('connected_agents', '?')}")

                # Pedir historial reciente
                since = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
                await ws.send(json.dumps({
                    "protocol_version": "0.1",
                    "message_type": "REQUEST_HISTORY",
                    "sender": AGENT_ID,
                    "context_id": "default",
                    "since_timestamp": since,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }))

                async def send_msg(content: str, room: str = "general"):
                    """Envía un mensaje al relay."""
                    try:
                        visible = clean_message(content)
                        if not visible:
                            return
                        await ws.send(json.dumps({
                            "protocol_version": "0.1",
                            "message_type": "MESSAGE",
                            "message_id": f"msg-{AGENT_ID}-{int(time.time() * 1000)}",
                            "sender": AGENT_ID,
                            "display_name": DISPLAY_NAME,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "content": visible,
                            "room": room,
                        }))
                    except Exception as e:
                        log(f"⚠️  Error enviando mensaje: {e}")

                seen_ids: set = set()
                hist_done = [False]

                # Tareas en segundo plano
                async def heartbeat():
                    while True:
                        await asyncio.sleep(12)
                        try:
                            await ws.send(json.dumps({
                                "protocol_version": "0.1",
                                "message_type": "PING",
                                "sender": AGENT_ID,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }))
                        except Exception:
                            break

                hb_task = asyncio.create_task(heartbeat())
                pa_task = asyncio.create_task(proactive_startup(send_msg))
                sm_task = asyncio.create_task(silence_monitor(send_msg))

                try:
                    async for raw_msg in ws:
                        try:
                            msg = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            continue

                        msg_type = msg.get("message_type", "")

                        # Ignorar pings y mensajes de sistema
                        if msg_type in ("PING", "PONG", "WELCOME", "HISTORY_END"):
                            if msg_type == "HISTORY_END":
                                hist_done[0] = True
                                log("📜 Historial cargado")
                            continue

                        # Deduplicar mensajes
                        msg_id = msg.get("message_id", "")
                        if msg_id and msg_id in seen_ids:
                            continue
                        if msg_id:
                            seen_ids.add(msg_id)

                        sender = msg.get("sender", "")
                        content = msg.get("content", "")
                        room = msg.get("room", "general")

                        # Ignorar nuestros propios mensajes
                        if sender == AGENT_ID:
                            continue

                        # Ignorar mensajes vacíos
                        if not content.strip():
                            continue

                        # Decidir si responder
                        my_name = DISPLAY_NAME.lower()
                        mentioned = my_name in content.lower() or AGENT_ID.lower() in content.lower()
                        should_reply = mentioned or (hist_done[0] and random.random() < 0.20)

                        if should_reply:
                            sender_display = msg.get("display_name", sender)
                            log(f"📨 Mensaje de {sender_display} en #{room}: {content[:60]}")
                            update_status("thinking")
                            await process_message(content, sender_display, send_msg, reply_room=room)

                except Exception as e:
                    log(f"⚠️  Error en bucle de mensajes: {e}")
                finally:
                    for task in [hb_task, pa_task, sm_task]:
                        task.cancel()

        except Exception as e:
            retries += 1
            wait = min(60, retries * 5)
            log(f"❌ Error de conexión: {e} — reintentando en {wait}s")
            update_status("reconnecting")
            await asyncio.sleep(wait)

# ══════════════════════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_dirs()
    log(f"🤖 {DISPLAY_NAME} ({AGENT_ID}) iniciando — v3 Plan-and-Execute")
    log(f"📁 Workspace: {WORKSPACE_DIR}")
    log(f"🔗 Relay: {RELAY_URL}")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log("👋 Agente detenido por el usuario.")
