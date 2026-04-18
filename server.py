from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import time
import webbrowser
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "app.db"
FOOD_SEED_PATH = BASE_DIR / "foods_seed.json"
COOKIE_NAME = "diet_tracker_session"

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = f"postgresql://{DATABASE_URL[len('postgres://'):]}"

IS_RENDER = os.environ.get("RENDER", "").lower() == "true"
USING_POSTGRES = bool(DATABASE_URL)


def env_bool(name: str, default: bool = False) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


APP_USERNAME = os.environ.get("APP_USERNAME", "admin").strip() or "admin"
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
APP_PASSWORD_HASH = os.environ.get("APP_PASSWORD_HASH", "")
AUTH_REQUIRED = env_bool(
    "AUTH_REQUIRED",
    default=IS_RENDER or bool(APP_PASSWORD) or bool(APP_PASSWORD_HASH),
)
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "1209600"))
FORCE_SECURE_COOKIE = env_bool("FORCE_SECURE_COOKIE", default=IS_RENDER)
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
if AUTH_REQUIRED and not SESSION_SECRET:
    SESSION_SECRET = secrets.token_urlsafe(48)

REMOTE_FOOD_PROVIDER = "Open Food Facts"
REMOTE_FOOD_LOOKUP_ENABLED = env_bool("REMOTE_FOOD_LOOKUP_ENABLED", default=True)
REMOTE_FOOD_SEARCH_URL = os.environ.get(
    "REMOTE_FOOD_SEARCH_URL",
    "https://search.openfoodfacts.org/search",
).strip()
REMOTE_FOOD_LOOKUP_LANGS = os.environ.get("REMOTE_FOOD_LOOKUP_LANGS", "zh,en").strip() or "zh,en"
REMOTE_FOOD_MIN_QUERY_LENGTH = max(
    2,
    int(os.environ.get("REMOTE_FOOD_MIN_QUERY_LENGTH", "2")),
)
REMOTE_FOOD_PAGE_SIZE = min(
    max(int(os.environ.get("REMOTE_FOOD_PAGE_SIZE", "6")), 1),
    8,
)
REMOTE_FOOD_FETCH_SIZE = min(max(REMOTE_FOOD_PAGE_SIZE * 4, 12), 24)
REMOTE_FOOD_CACHE_TTL_SECONDS = max(
    3600,
    int(os.environ.get("REMOTE_FOOD_CACHE_TTL_SECONDS", str(7 * 24 * 60 * 60))),
)
REMOTE_FOOD_LOOKUP_TIMEOUT_SECONDS = max(
    3,
    int(os.environ.get("REMOTE_FOOD_LOOKUP_TIMEOUT_SECONDS", "12")),
)
REMOTE_FOOD_USER_AGENT = os.environ.get(
    "REMOTE_FOOD_USER_AGENT",
    "diet-weight-tracker/1.0 (+https://github.com/forest70kj/diet-weight-tracker)",
).strip()
REMOTE_QUERY_VARIANT_MAP = {
    "奥利奥": ["oreo"],
    "可口可乐": ["coca cola", "coke"],
    "百事可乐": ["pepsi"],
    "士力架": ["snickers"],
    "奇巧": ["kitkat", "kit kat"],
    "乐事": ["lays", "potato chips"],
    "m豆": ["m&m", "m and m"],
    "蛋白棒": ["protein bar"],
    "蛋白粉": ["protein powder"],
    "鸡胸肉": ["chicken breast"],
}


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def iso_today() -> str:
    return date.today().isoformat()


def parse_date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def parse_utc_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")


def get_lan_ip() -> str:
    for interface in ("en0", "en1", "bridge0", "en2", "en3"):
        try:
            result = subprocess.run(
                ["ipconfig", "getifaddr", interface],
                check=False,
                capture_output=True,
                text=True,
            )
            candidate = result.stdout.strip()
            if candidate and not candidate.startswith("127."):
                return candidate
        except OSError:
            continue

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            candidate = sock.getsockname()[0]
            if candidate and not candidate.startswith("127."):
                return candidate
    except OSError:
        pass

    return "127.0.0.1"


def storage_mode_label() -> str:
    return "PostgreSQL" if USING_POSTGRES else "SQLite"


def deploy_mode_label() -> str:
    return "Render" if IS_RENDER else "Local"


def validate_runtime_config() -> None:
    if USING_POSTGRES and psycopg is None:
        raise RuntimeError(
            "检测到 DATABASE_URL，但当前环境没有安装 psycopg。"
            " 请先执行 `pip install -r requirements.txt`。"
        )

    if AUTH_REQUIRED and not (APP_PASSWORD or APP_PASSWORD_HASH):
        raise RuntimeError(
            "启用了登录保护，但没有配置 APP_PASSWORD 或 APP_PASSWORD_HASH。"
        )


@contextmanager
def get_connection():
    if USING_POSTGRES:
        connection = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(DB_PATH)
        connection.row_factory = sqlite3.Row

    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def sql(query: str) -> str:
    return query if not USING_POSTGRES else query.replace("?", "%s")


def execute(connection, query: str, params: tuple = ()):
    return connection.execute(sql(query), params)


def seed_foods(connection) -> None:
    foods = json.loads(FOOD_SEED_PATH.read_text(encoding="utf-8"))
    if USING_POSTGRES:
        query = """
            INSERT INTO foods (
                name,
                category,
                calories,
                basis_amount,
                basis_unit,
                aliases
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (name) DO NOTHING
        """
    else:
        query = """
            INSERT OR IGNORE INTO foods (
                name,
                category,
                calories,
                basis_amount,
                basis_unit,
                aliases
            ) VALUES (?, ?, ?, ?, ?, ?)
        """

    for item in foods:
        execute(
            connection,
            query,
            (
                item["name"],
                item.get("category", ""),
                item["calories"],
                item["basis_amount"],
                item["basis_unit"],
                item.get("aliases", ""),
            ),
        )


def init_db() -> None:
    sqlite_statements = [
        """
        CREATE TABLE IF NOT EXISTS foods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL DEFAULT '',
            calories REAL NOT NULL,
            basis_amount REAL NOT NULL,
            basis_unit TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_date TEXT NOT NULL,
            meal_type TEXT NOT NULL,
            food_name TEXT NOT NULL,
            amount REAL NOT NULL,
            basis_amount REAL NOT NULL,
            basis_unit TEXT NOT NULL,
            calories_per_basis REAL NOT NULL,
            total_calories REAL NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS weights (
            record_date TEXT PRIMARY KEY,
            weight REAL NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS food_lookup_cache (
            query_key TEXT PRIMARY KEY,
            query_text TEXT NOT NULL,
            results_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_meals_record_date ON meals(record_date)",
        "CREATE INDEX IF NOT EXISTS idx_meals_created_at ON meals(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_weights_record_date ON weights(record_date)",
        "CREATE INDEX IF NOT EXISTS idx_food_lookup_cache_fetched_at ON food_lookup_cache(fetched_at)",
    ]

    postgres_statements = [
        """
        CREATE TABLE IF NOT EXISTS foods (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL DEFAULT '',
            calories DOUBLE PRECISION NOT NULL,
            basis_amount DOUBLE PRECISION NOT NULL,
            basis_unit TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS meals (
            id BIGSERIAL PRIMARY KEY,
            record_date TEXT NOT NULL,
            meal_type TEXT NOT NULL,
            food_name TEXT NOT NULL,
            amount DOUBLE PRECISION NOT NULL,
            basis_amount DOUBLE PRECISION NOT NULL,
            basis_unit TEXT NOT NULL,
            calories_per_basis DOUBLE PRECISION NOT NULL,
            total_calories DOUBLE PRECISION NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS weights (
            record_date TEXT PRIMARY KEY,
            weight DOUBLE PRECISION NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS food_lookup_cache (
            query_key TEXT PRIMARY KEY,
            query_text TEXT NOT NULL,
            results_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_meals_record_date ON meals(record_date)",
        "CREATE INDEX IF NOT EXISTS idx_meals_created_at ON meals(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_weights_record_date ON weights(record_date)",
        "CREATE INDEX IF NOT EXISTS idx_food_lookup_cache_fetched_at ON food_lookup_cache(fetched_at)",
    ]

    statements = postgres_statements if USING_POSTGRES else sqlite_statements

    with get_connection() as connection:
        for statement in statements:
            execute(connection, statement)
        seed_foods(connection)


def json_response(
    handler: BaseHTTPRequestHandler,
    payload: dict,
    status: int = 200,
    extra_headers: Optional[List[Tuple[str, str]]] = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    if extra_headers:
        for name, value in extra_headers:
            handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


def error_response(handler: BaseHTTPRequestHandler, message: str, status: int = 400) -> None:
    json_response(handler, {"error": message}, status=status)


def read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    content_length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(content_length) if content_length else b"{}"
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("请求体不是有效的 JSON") from exc


def serialize_food(
    row,
    *,
    source: str = "local",
    source_label: str = "本地",
    brand: str = "",
    source_detail: str = "常用食物库",
) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "category": row["category"],
        "calories": row["calories"],
        "basis_amount": row["basis_amount"],
        "basis_unit": row["basis_unit"],
        "aliases": row["aliases"],
        "source": source,
        "source_label": source_label,
        "brand": brand,
        "source_detail": source_detail,
    }


def serialize_meal(row) -> dict:
    return {
        "id": row["id"],
        "record_date": row["record_date"],
        "meal_type": row["meal_type"],
        "food_name": row["food_name"],
        "amount": row["amount"],
        "basis_amount": row["basis_amount"],
        "basis_unit": row["basis_unit"],
        "calories_per_basis": row["calories_per_basis"],
        "total_calories": row["total_calories"],
        "note": row["note"],
        "created_at": row["created_at"],
    }


def serialize_weight(row) -> dict:
    return {
        "record_date": row["record_date"],
        "weight": row["weight"],
        "note": row["note"],
        "updated_at": row["updated_at"],
    }


def to_float(value: object, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 需要是数字") from exc


def validate_positive(value: float, field_name: str) -> float:
    if value <= 0:
        raise ValueError(f"{field_name} 必须大于 0")
    return value


def format_decimal(value: float) -> float:
    return round(value, 2)


def calculate_total_calories(amount: float, basis_amount: float, calories: float) -> float:
    return format_decimal((amount / basis_amount) * calories)


def normalize_food_query(query: str) -> str:
    return " ".join(str(query).split()).strip().lower()


def parse_optional_float(value: object) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return format_decimal(number)


def join_brands(value: object) -> str:
    if isinstance(value, list):
        brands = [str(item).strip() for item in value if str(item).strip()]
        return " / ".join(brands)
    return str(value or "").strip()


def merge_aliases(*values: object) -> str:
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "")
        text = re.sub(r"[，,、/]+", " ", text)
        for chunk in text.split():
            token = chunk.strip()
            if not token:
                continue
            token_key = token.lower()
            if token_key in seen:
                continue
            seen.add(token_key)
            aliases.append(token)
    return " ".join(aliases)


def infer_basis_unit(quantity: str, serving_unit: str = "") -> str:
    sample = f"{quantity} {serving_unit}".lower()
    if any(keyword in sample for keyword in ("ml", "毫升", " l", "升")):
        return "ml"
    return "g"


def remote_result_matches_query(query: str, *values: object) -> bool:
    query_key = normalize_food_query(query)
    searchable_text = normalize_food_query(" ".join(str(value or "") for value in values))
    if not query_key or not searchable_text:
        return False

    compact_query = query_key.replace(" ", "")
    compact_text = searchable_text.replace(" ", "")
    if compact_query and compact_query in compact_text:
        return True

    tokens = [token for token in query_key.split() if token]
    if not tokens:
        return False

    non_cjk_tokens = [
        token
        for token in tokens
        if not any("\u4e00" <= character <= "\u9fff" for character in token)
    ]
    comparable_tokens = [token for token in non_cjk_tokens if len(token) > 1] or non_cjk_tokens
    if not comparable_tokens:
        return False

    return all(token in searchable_text for token in comparable_tokens)


def build_remote_query_variants(query: str) -> list[str]:
    trimmed_query = query.strip()
    if not trimmed_query:
        return []

    variants: list[str] = [trimmed_query]
    mapped_variants = REMOTE_QUERY_VARIANT_MAP.get(trimmed_query, [])
    for variant in mapped_variants:
        candidate = variant.strip()
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def get_local_foods(query: str) -> list[dict]:
    like_operator = "ILIKE" if USING_POSTGRES else "LIKE"
    like = f"%{query.strip()}%"

    with get_connection() as connection:
        if query.strip():
            rows = execute(
                connection,
                f"""
                SELECT id, name, category, calories, basis_amount, basis_unit, aliases
                FROM foods
                WHERE name {like_operator} ? OR aliases {like_operator} ?
                ORDER BY
                    CASE
                        WHEN name = ? THEN 0
                        WHEN name {like_operator} ? THEN 1
                        WHEN aliases {like_operator} ? THEN 2
                        ELSE 3
                    END,
                    name ASC
                LIMIT 12
                """,
                (like, like, query.strip(), f"{query.strip()}%", like),
            ).fetchall()
        else:
            rows = execute(
                connection,
                """
                SELECT id, name, category, calories, basis_amount, basis_unit, aliases
                FROM foods
                ORDER BY category ASC, name ASC
                LIMIT 12
                """,
            ).fetchall()
    return [serialize_food(row) for row in rows]


def load_cached_remote_foods(query: str) -> Optional[list[dict]]:
    query_key = normalize_food_query(query)
    if not query_key:
        return None

    with get_connection() as connection:
        row = execute(
            connection,
            """
            SELECT results_json, fetched_at
            FROM food_lookup_cache
            WHERE query_key = ?
            """,
            (query_key,),
        ).fetchone()

    if not row:
        return None

    try:
        fetched_at = parse_utc_timestamp(row["fetched_at"])
    except (TypeError, ValueError):
        return None

    if (datetime.utcnow() - fetched_at).total_seconds() > REMOTE_FOOD_CACHE_TTL_SECONDS:
        return None

    try:
        cached_foods = json.loads(row["results_json"])
    except json.JSONDecodeError:
        return None

    foods: list[dict] = []
    raw_count = 0
    for food in cached_foods:
        if not isinstance(food, dict):
            continue
        raw_count += 1
        if not remote_result_matches_query(
            query,
            food.get("name", ""),
            food.get("aliases", ""),
            food.get("brand", ""),
        ):
            continue
        restored = dict(food)
        restored["source"] = "remote_cache"
        restored["source_label"] = "联网缓存"
        restored["source_detail"] = REMOTE_FOOD_PROVIDER
        foods.append(restored)
    if raw_count and not foods:
        return None
    return foods


def save_remote_food_cache(query: str, foods: list[dict]) -> None:
    query_key = normalize_food_query(query)
    if not query_key:
        return

    with get_connection() as connection:
        execute(
            connection,
            """
            INSERT INTO food_lookup_cache (query_key, query_text, results_json, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(query_key) DO UPDATE SET
                query_text = excluded.query_text,
                results_json = excluded.results_json,
                fetched_at = excluded.fetched_at
            """,
            (
                query_key,
                query.strip(),
                json.dumps(foods, ensure_ascii=False),
                utc_now(),
            ),
        )


def extract_remote_food_basis(product: dict) -> Optional[dict]:
    nutriments = product.get("nutriments") or {}
    quantity = str(product.get("quantity", "")).strip()
    serving_unit = str(product.get("serving_quantity_unit", "")).strip()
    basis_unit = infer_basis_unit(quantity, serving_unit)

    kcal_per_100 = parse_optional_float(
        nutriments.get("energy-kcal_100g") or nutriments.get("energy-kcal")
    )
    if kcal_per_100:
        return {
            "calories": kcal_per_100,
            "basis_amount": 100.0,
            "basis_unit": basis_unit,
        }

    energy_per_100 = parse_optional_float(
        nutriments.get("energy_100g") or nutriments.get("energy")
    )
    if energy_per_100:
        return {
            "calories": format_decimal(energy_per_100 / 4.184),
            "basis_amount": 100.0,
            "basis_unit": basis_unit,
        }

    serving_amount = parse_optional_float(product.get("serving_quantity"))
    kcal_per_serving = parse_optional_float(nutriments.get("energy-kcal_serving"))
    if kcal_per_serving and serving_amount:
        return {
            "calories": kcal_per_serving,
            "basis_amount": serving_amount,
            "basis_unit": serving_unit or basis_unit,
        }

    energy_per_serving = parse_optional_float(nutriments.get("energy_serving"))
    if energy_per_serving and serving_amount:
        return {
            "calories": format_decimal(energy_per_serving / 4.184),
            "basis_amount": serving_amount,
            "basis_unit": serving_unit or basis_unit,
        }

    return None


def normalize_remote_food(product: dict, query: str, query_variant: str, index: int) -> Optional[dict]:
    raw_name = str(
        product.get("product_name_zh")
        or product.get("product_name")
        or product.get("product_name_en")
        or ""
    ).strip()
    if not raw_name:
        return None

    basis = extract_remote_food_basis(product)
    if not basis:
        return None

    brand = join_brands(product.get("brands"))
    if not remote_result_matches_query(query_variant, raw_name, brand):
        return None

    display_name = raw_name
    if brand and brand.lower() not in raw_name.lower():
        display_name = f"{raw_name}（{brand}）"

    return {
        "id": f"remote:{normalize_food_query(query).replace(' ', '-') or 'food'}:{index}",
        "name": display_name,
        "category": "联网查询",
        "calories": basis["calories"],
        "basis_amount": basis["basis_amount"],
        "basis_unit": basis["basis_unit"],
        "aliases": merge_aliases(query, query_variant, raw_name, brand),
        "source": "remote",
        "source_label": "联网",
        "brand": brand,
        "source_detail": REMOTE_FOOD_PROVIDER,
    }


def fetch_remote_foods(query: str) -> list[dict]:
    remote_foods: list[dict] = []
    seen_names: set[str] = set()
    for query_variant in build_remote_query_variants(query):
        params = urlencode(
            {
                "q": query_variant,
                "langs": REMOTE_FOOD_LOOKUP_LANGS,
                "page_size": REMOTE_FOOD_FETCH_SIZE,
                "fields": (
                    "product_name,product_name_zh,product_name_en,brands,nutriments,"
                    "quantity,serving_quantity,serving_quantity_unit"
                ),
            }
        )
        url = f"{REMOTE_FOOD_SEARCH_URL}?{params}"
        request = Request(
            url,
            headers={
                "User-Agent": REMOTE_FOOD_USER_AGENT,
                "Accept": "application/json",
            },
        )

        try:
            with urlopen(request, timeout=REMOTE_FOOD_LOOKUP_TIMEOUT_SECONDS) as response:
                payload = json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError("联网热量查询暂时不可用，请稍后再试或先手动填写。") from exc

        hits = payload.get("hits") or payload.get("products") or []
        for index, hit in enumerate(hits):
            product = hit.get("product") if isinstance(hit, dict) else None
            normalized = normalize_remote_food(product or hit, query, query_variant, index)
            if not normalized:
                continue
            dedupe_key = normalize_food_query(normalized["name"])
            if dedupe_key in seen_names:
                continue
            seen_names.add(dedupe_key)
            remote_foods.append(normalized)
            if len(remote_foods) >= REMOTE_FOOD_PAGE_SIZE:
                return remote_foods
    return remote_foods


def get_foods(query: str, allow_remote: bool = False) -> dict:
    trimmed_query = query.strip()
    local_foods = get_local_foods(trimmed_query)
    if local_foods:
        return {"foods": local_foods, "source": "local", "message": ""}

    if not trimmed_query:
        return {"foods": [], "source": "none", "message": ""}

    if (
        not allow_remote
        or not REMOTE_FOOD_LOOKUP_ENABLED
        or len(trimmed_query) < REMOTE_FOOD_MIN_QUERY_LENGTH
    ):
        return {
            "foods": [],
            "source": "none",
            "message": (
                "再多输几个字，我会在本地没找到时自动联网补查热量。"
                if len(trimmed_query) < REMOTE_FOOD_MIN_QUERY_LENGTH
                else ""
            ),
        }

    cached_foods = load_cached_remote_foods(trimmed_query)
    if cached_foods is not None:
        return {
            "foods": cached_foods,
            "source": "remote_cache",
            "message": f"本地没找到，已从联网缓存里找到 {len(cached_foods)} 个结果。",
        }

    remote_foods = fetch_remote_foods(trimmed_query)
    save_remote_food_cache(trimmed_query, remote_foods)
    if remote_foods:
        return {
            "foods": remote_foods,
            "source": "remote",
            "message": f"本地没找到，已联网查到 {len(remote_foods)} 个结果。",
        }

    return {
        "foods": [],
        "source": "none",
        "message": "本地和网络都没找到，先切到手动热量模式也可以。",
    }


def upsert_custom_food(
    connection,
    name: str,
    calories: float,
    basis_amount: float,
    basis_unit: str,
) -> None:
    execute(
        connection,
        """
        INSERT INTO foods (name, category, calories, basis_amount, basis_unit, aliases)
        VALUES (?, '自定义', ?, ?, ?, '')
        ON CONFLICT(name) DO UPDATE SET
            category = excluded.category,
            calories = excluded.calories,
            basis_amount = excluded.basis_amount,
            basis_unit = excluded.basis_unit
        """,
        (name, calories, basis_amount, basis_unit),
    )


def create_meal(payload: dict) -> dict:
    record_date = parse_date(str(payload.get("record_date", iso_today())))
    meal_type = str(payload.get("meal_type", "加餐")).strip() or "加餐"
    food_name = str(payload.get("food_name", "")).strip()
    basis_unit = str(payload.get("basis_unit", "")).strip()
    note = str(payload.get("note", "")).strip()
    save_custom_food = bool(payload.get("save_custom_food"))

    if not food_name:
        raise ValueError("请填写食物名称")
    if not basis_unit:
        raise ValueError("请填写热量单位")

    amount = validate_positive(to_float(payload.get("amount"), "食用量"), "食用量")
    basis_amount = validate_positive(
        to_float(payload.get("basis_amount"), "热量基准量"),
        "热量基准量",
    )
    calories_per_basis = validate_positive(
        to_float(payload.get("calories_per_basis"), "热量"),
        "热量",
    )
    total_calories = calculate_total_calories(amount, basis_amount, calories_per_basis)

    created_at = utc_now()
    with get_connection() as connection:
        if save_custom_food:
            upsert_custom_food(
                connection,
                food_name,
                calories_per_basis,
                basis_amount,
                basis_unit,
            )

        insert_query = """
            INSERT INTO meals (
                record_date,
                meal_type,
                food_name,
                amount,
                basis_amount,
                basis_unit,
                calories_per_basis,
                total_calories,
                note,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        if USING_POSTGRES:
            cursor = execute(
                connection,
                f"{insert_query} RETURNING id",
                (
                    record_date,
                    meal_type,
                    food_name,
                    amount,
                    basis_amount,
                    basis_unit,
                    calories_per_basis,
                    total_calories,
                    note,
                    created_at,
                ),
            )
            inserted_row = cursor.fetchone()
            meal_id = inserted_row["id"]
        else:
            cursor = execute(
                connection,
                insert_query,
                (
                    record_date,
                    meal_type,
                    food_name,
                    amount,
                    basis_amount,
                    basis_unit,
                    calories_per_basis,
                    total_calories,
                    note,
                    created_at,
                ),
            )
            meal_id = cursor.lastrowid

        meal = execute(
            connection,
            """
            SELECT *
            FROM meals
            WHERE id = ?
            """,
            (meal_id,),
        ).fetchone()

    return serialize_meal(meal)


def delete_meal(meal_id: int) -> None:
    with get_connection() as connection:
        cursor = execute(connection, "DELETE FROM meals WHERE id = ?", (meal_id,))
        if cursor.rowcount == 0:
            raise LookupError("没有找到要删除的饮食记录")


def upsert_weight(payload: dict) -> dict:
    record_date = parse_date(str(payload.get("record_date", iso_today())))
    note = str(payload.get("note", "")).strip()
    weight = validate_positive(to_float(payload.get("weight"), "体重"), "体重")
    updated_at = utc_now()

    with get_connection() as connection:
        execute(
            connection,
            """
            INSERT INTO weights (record_date, weight, note, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(record_date) DO UPDATE SET
                weight = excluded.weight,
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            (record_date, weight, note, updated_at),
        )
        row = execute(
            connection,
            "SELECT * FROM weights WHERE record_date = ?",
            (record_date,),
        ).fetchone()
    return serialize_weight(row)


def delete_weight(record_date: str) -> None:
    parsed = parse_date(record_date)
    with get_connection() as connection:
        cursor = execute(
            connection,
            "DELETE FROM weights WHERE record_date = ?",
            (parsed,),
        )
        if cursor.rowcount == 0:
            raise LookupError("没有找到要删除的体重记录")


def get_dashboard(selected_date: str, days: int) -> dict:
    parsed_date = date.fromisoformat(parse_date(selected_date))
    range_days = min(max(days, 7), 180)
    start_date = (parsed_date - timedelta(days=range_days - 1)).isoformat()
    week_start = (parsed_date - timedelta(days=6)).isoformat()

    with get_connection() as connection:
        meals = execute(
            connection,
            """
            SELECT *
            FROM meals
            WHERE record_date = ?
            ORDER BY created_at DESC, id DESC
            """,
            (parsed_date.isoformat(),),
        ).fetchall()

        breakdown_rows = execute(
            connection,
            """
            SELECT meal_type, SUM(total_calories) AS total
            FROM meals
            WHERE record_date = ?
            GROUP BY meal_type
            ORDER BY
                CASE meal_type
                    WHEN '早餐' THEN 1
                    WHEN '午餐' THEN 2
                    WHEN '晚餐' THEN 3
                    WHEN '加餐' THEN 4
                    ELSE 5
                END
            """,
            (parsed_date.isoformat(),),
        ).fetchall()

        today_total = execute(
            connection,
            """
            SELECT COALESCE(SUM(total_calories), 0) AS total
            FROM meals
            WHERE record_date = ?
            """,
            (parsed_date.isoformat(),),
        ).fetchone()["total"]

        selected_weight = execute(
            connection,
            "SELECT * FROM weights WHERE record_date = ?",
            (parsed_date.isoformat(),),
        ).fetchone()

        latest_weight = execute(
            connection,
            """
            SELECT *
            FROM weights
            ORDER BY record_date DESC
            LIMIT 1
            """,
        ).fetchone()

        weights_window = execute(
            connection,
            """
            SELECT *
            FROM weights
            WHERE record_date BETWEEN ? AND ?
            ORDER BY record_date ASC
            """,
            (start_date, parsed_date.isoformat()),
        ).fetchall()

        recent_weights = execute(
            connection,
            """
            SELECT *
            FROM weights
            ORDER BY record_date DESC
            LIMIT 8
            """,
        ).fetchall()

        calorie_series_rows = execute(
            connection,
            """
            SELECT record_date, SUM(total_calories) AS total
            FROM meals
            WHERE record_date BETWEEN ? AND ?
            GROUP BY record_date
            ORDER BY record_date ASC
            """,
            (start_date, parsed_date.isoformat()),
        ).fetchall()

        calories_week = execute(
            connection,
            """
            SELECT COALESCE(AVG(day_total), 0) AS avg_total
            FROM (
                SELECT record_date, SUM(total_calories) AS day_total
                FROM meals
                WHERE record_date BETWEEN ? AND ?
                GROUP BY record_date
            ) daily_totals
            """,
            (week_start, parsed_date.isoformat()),
        ).fetchone()["avg_total"]

        weights_week = execute(
            connection,
            """
            SELECT record_date, weight
            FROM weights
            WHERE record_date BETWEEN ? AND ?
            ORDER BY record_date ASC
            """,
            (week_start, parsed_date.isoformat()),
        ).fetchall()

    breakdown = [
        {"meal_type": row["meal_type"], "total": format_decimal(row["total"])}
        for row in breakdown_rows
    ]

    weight_change_7d = None
    if len(weights_week) >= 2:
        weight_change_7d = format_decimal(weights_week[-1]["weight"] - weights_week[0]["weight"])

    since_first_change = None
    if len(weights_window) >= 2:
        since_first_change = format_decimal(weights_window[-1]["weight"] - weights_window[0]["weight"])

    return {
        "selected_date": parsed_date.isoformat(),
        "today": {
            "total_calories": format_decimal(today_total),
            "meal_count": len(meals),
            "breakdown": breakdown,
            "weight": serialize_weight(selected_weight) if selected_weight else None,
        },
        "stats": {
            "latest_weight": serialize_weight(latest_weight) if latest_weight else None,
            "average_calories_7d": format_decimal(calories_week),
            "weight_change_7d": weight_change_7d,
            "weight_change_in_range": since_first_change,
        },
        "meals": [serialize_meal(row) for row in meals],
        "weight_history": [serialize_weight(row) for row in weights_window],
        "recent_weights": [serialize_weight(row) for row in recent_weights],
        "calorie_history": [
            {"record_date": row["record_date"], "total_calories": format_decimal(row["total"])}
            for row in calorie_series_rows
        ],
    }


def b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}".encode("utf-8"))


def generate_password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 210000
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${b64_encode(salt)}${b64_encode(derived)}"


def verify_password(password: str) -> bool:
    if APP_PASSWORD_HASH:
        try:
            algorithm, raw_iterations, salt_b64, digest_b64 = APP_PASSWORD_HASH.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            iterations = int(raw_iterations)
            expected_digest = b64_decode(digest_b64)
            salt = b64_decode(salt_b64)
        except (ValueError, TypeError):
            return False

        actual_digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations,
        )
        return hmac.compare_digest(actual_digest, expected_digest)

    return hmac.compare_digest(password, APP_PASSWORD)


def authenticate_credentials(username: str, password: str) -> bool:
    if not AUTH_REQUIRED:
        return True
    if username != APP_USERNAME:
        return False
    return verify_password(password)


def sign_value(value: str) -> str:
    signature = hmac.new(
        SESSION_SECRET.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{value}.{signature}"


def unsign_value(value: str) -> Optional[str]:
    try:
        payload, signature = value.rsplit(".", 1)
    except ValueError:
        return None

    expected_signature = hmac.new(
        SESSION_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        return None
    return payload


def create_session_token(username: str) -> str:
    payload = {
        "u": username,
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }
    encoded = b64_encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    return sign_value(encoded)


def verify_session_token(token: str) -> Optional[str]:
    if not token or not SESSION_SECRET:
        return None

    unsigned = unsign_value(token)
    if not unsigned:
        return None

    try:
        payload = json.loads(b64_decode(unsigned).decode("utf-8"))
    except (json.JSONDecodeError, ValueError, TypeError):
        return None

    if payload.get("exp", 0) < int(time.time()):
        return None

    username = str(payload.get("u", "")).strip()
    if username != APP_USERNAME:
        return None
    return username


def build_cookie_header(token: str, secure: bool, max_age: int) -> str:
    cookie = SimpleCookie()
    cookie[COOKIE_NAME] = token
    morsel = cookie[COOKIE_NAME]
    morsel["path"] = "/"
    morsel["httponly"] = True
    morsel["max-age"] = str(max_age)
    morsel["samesite"] = "Lax"
    if secure:
        morsel["secure"] = True
    return morsel.OutputString()


def clear_cookie_header(secure: bool) -> str:
    return build_cookie_header("", secure=secure, max_age=0)


def build_session_payload(username: Optional[str]) -> dict:
    authenticated = not AUTH_REQUIRED or bool(username)
    effective_username = username or (APP_USERNAME if authenticated else "")
    return {
        "authenticated": authenticated,
        "auth_required": AUTH_REQUIRED,
        "username": effective_username,
        "login_hint": APP_USERNAME if AUTH_REQUIRED else "",
        "storage_mode": storage_mode_label(),
        "deploy_mode": deploy_mode_label(),
    }


class AppHandler(BaseHTTPRequestHandler):
    public_api_paths = {"/api/session", "/api/login", "/api/logout", "/api/health"}

    def current_user(self) -> Optional[str]:
        if not AUTH_REQUIRED:
            return APP_USERNAME

        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return None

        cookie = SimpleCookie()
        cookie.load(raw_cookie)
        morsel = cookie.get(COOKIE_NAME)
        if morsel is None:
            return None

        return verify_session_token(morsel.value)

    def is_secure_request(self) -> bool:
        if FORCE_SECURE_COOKIE:
            return True
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def require_auth(self) -> bool:
        if not AUTH_REQUIRED:
            return True
        if self.current_user():
            return True

        error_response(self, "请先登录后再继续", status=401)
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/session":
            json_response(self, build_session_payload(self.current_user()))
            return

        if parsed.path == "/api/health":
            json_response(
                self,
                {
                    "ok": True,
                    "auth_required": AUTH_REQUIRED,
                    "storage_mode": storage_mode_label(),
                    "deploy_mode": deploy_mode_label(),
                },
            )
            return

        if parsed.path.startswith("/api/") and not self.require_auth():
            return

        if parsed.path == "/api/foods":
            query = parse_qs(parsed.query).get("query", [""])[0]
            allow_remote = parse_qs(parsed.query).get("allow_remote", ["0"])[0].strip().lower()
            json_response(
                self,
                get_foods(query, allow_remote=allow_remote in {"1", "true", "yes", "on"}),
            )
            return

        if parsed.path == "/api/dashboard":
            params = parse_qs(parsed.query)
            selected_date = params.get("date", [iso_today()])[0]
            days = int(params.get("days", ["30"])[0])
            try:
                payload = get_dashboard(selected_date, days)
            except ValueError as exc:
                error_response(self, str(exc), status=400)
                return
            json_response(self, payload)
            return

        self.serve_static(parsed.path)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        self.serve_static(parsed.path, send_body=False)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        try:
            payload = read_json_body(self)
        except ValueError as exc:
            error_response(self, str(exc), status=400)
            return

        if parsed.path == "/api/login":
            if not AUTH_REQUIRED:
                json_response(self, build_session_payload(APP_USERNAME))
                return

            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", ""))
            if not authenticate_credentials(username, password):
                error_response(self, "用户名或密码不正确", status=401)
                return

            token = create_session_token(username)
            headers = [
                (
                    "Set-Cookie",
                    build_cookie_header(
                        token,
                        secure=self.is_secure_request(),
                        max_age=SESSION_TTL_SECONDS,
                    ),
                )
            ]
            json_response(
                self,
                build_session_payload(username),
                status=200,
                extra_headers=headers,
            )
            return

        if parsed.path == "/api/logout":
            headers = [
                (
                    "Set-Cookie",
                    clear_cookie_header(secure=self.is_secure_request()),
                )
            ]
            json_response(
                self,
                build_session_payload(None),
                status=200,
                extra_headers=headers,
            )
            return

        if not self.require_auth():
            return

        try:
            if parsed.path == "/api/meals":
                meal = create_meal(payload)
                json_response(self, {"meal": meal}, status=201)
                return

            if parsed.path == "/api/weights":
                weight = upsert_weight(payload)
                json_response(self, {"weight": weight}, status=201)
                return
        except ValueError as exc:
            error_response(self, str(exc), status=400)
            return

        error_response(self, "未找到接口", status=404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)

        if not self.require_auth():
            return

        try:
            if parsed.path.startswith("/api/meals/"):
                meal_id = int(parsed.path.rsplit("/", 1)[-1])
                delete_meal(meal_id)
                json_response(self, {"ok": True})
                return

            if parsed.path.startswith("/api/weights/"):
                record_date = parsed.path.rsplit("/", 1)[-1]
                delete_weight(record_date)
                json_response(self, {"ok": True})
                return
        except ValueError as exc:
            error_response(self, str(exc), status=400)
            return
        except LookupError as exc:
            error_response(self, str(exc), status=404)
            return

        error_response(self, "未找到接口", status=404)

    def serve_static(self, raw_path: str, send_body: bool = True) -> None:
        if raw_path in ("", "/"):
            relative_path = "index.html"
        elif raw_path == "/sw.js":
            relative_path = "sw.js"
        elif raw_path == "/favicon.ico":
            relative_path = "icons/favicon-192.png"
        elif raw_path.startswith("/static/"):
            relative_path = raw_path.removeprefix("/static/")
        else:
            relative_path = raw_path.lstrip("/")

        file_path = (STATIC_DIR / relative_path).resolve()

        if STATIC_DIR not in file_path.parents and file_path != STATIC_DIR / "index.html":
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        mime_type, _ = mimetypes.guess_type(str(file_path))
        if file_path.suffix == ".webmanifest":
            mime_type = "application/manifest+json"
        body = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime_type or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="饮食与体重记录应用")
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0" if IS_RENDER else "127.0.0.1"),
        help="监听地址",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "10000" if IS_RENDER else "8766")),
        help="监听端口",
    )
    parser.add_argument("--open-browser", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument(
        "--print-password-hash",
        metavar="PASSWORD",
        help="生成密码哈希后退出，可用于 APP_PASSWORD_HASH",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    if args.print_password_hash is not None:
        print(generate_password_hash(args.print_password_hash))
        return

    validate_runtime_config()
    init_db()

    server = ThreadingHTTPServer((args.host, args.port), AppHandler)

    print("饮食与体重记录应用已启动")
    print(f"部署模式：{deploy_mode_label()} · 存储：{storage_mode_label()}")
    print(f"登录保护：{'已开启' if AUTH_REQUIRED else '未开启'}")

    if IS_RENDER:
        print(f"已监听 Render 端口：{args.host}:{args.port}")
    else:
        lan_ip = get_lan_ip()
        local_url = f"http://127.0.0.1:{args.port}"
        lan_url = f"http://{lan_ip}:{args.port}"
        print(f"电脑本机访问：{local_url}")
        if args.host in ("0.0.0.0", "::"):
            print(f"同一 Wi-Fi 下手机访问：{lan_url}")
        else:
            print("如需手机访问，请用 --host 0.0.0.0 重新启动")

        if args.open_browser:
            webbrowser.open(local_url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
