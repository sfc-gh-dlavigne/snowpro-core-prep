import streamlit as st
import random
import time
import json
import os
import re
from datetime import datetime
import streamlit.components.v1 as components

PROGRESS_FILE = os.path.join(os.path.dirname(__file__), "progress.json")
QUESTIONS_CACHE_FILE = os.path.join(os.path.dirname(__file__), "questions_cache.json")
TIMING_LOG_FILE = os.path.join(os.path.dirname(__file__), "preload_timing.log")


def load_progress():
    """Load persisted progress from disk. Returns dict or empty defaults."""
    if not os.path.exists(PROGRESS_FILE):
        return {}
    try:
        with open(PROGRESS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_progress():
    """Persist current progress stats to disk."""
    data = {
        "domain_stats_study": st.session_state.domain_stats_study,
        "domain_stats_exam": st.session_state.domain_stats_exam,
        "total_questions_answered": st.session_state.total_questions_answered,
        "total_correct": st.session_state.total_correct,
        "exam_history": st.session_state.exam_history,
    }
    try:
        with open(PROGRESS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _cache_key_to_str(k) -> str:
    """Serialize cache key to JSON-safe string. Handles both int and (int, int) tuple."""
    if isinstance(k, tuple):
        return f"{k[0]}:{k[1]}"
    return f"{k}:0"


def _str_to_cache_key(s: str) -> tuple:
    """Deserialize JSON string key back to (concept_idx, variation) tuple."""
    parts = s.split(":", 1)
    return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)


def save_questions_cache():
    """Persist generated question cache to disk."""
    data = {_cache_key_to_str(k): v for k, v in st.session_state.generated_questions.items()}
    try:
        with open(QUESTIONS_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def write_timing_log(context: str, phase: str, detail: str):
    """Append a timestamped timing entry to preload_timing.log."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"{ts} | {context} | phase={phase:<8} | {detail}\n"
    try:
        with open(TIMING_LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


def load_questions_cache() -> dict:
    """Load generated question cache from disk. Returns empty dict on any error."""
    if not os.path.exists(QUESTIONS_CACHE_FILE):
        return {}
    try:
        with open(QUESTIONS_CACHE_FILE, "r") as f:
            raw = json.load(f)
        return {_str_to_cache_key(k): v for k, v in raw.items()}
    except Exception:
        return {}


def log_error(message: str):
    """Append an error to the session log and increment unseen count."""
    context = st.session_state.get("error_context", "")
    prefix = f"[{context}] " if context else ""
    st.session_state.error_log.append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "message": prefix + message,
    })
    st.session_state.error_unseen_count += 1


_LAST_CONN_ERROR = ""   # most recent reason get_snowflake_connection() failed


@st.cache_resource
def get_snowflake_connection():
    """
    Connection priority:
    1. Streamlit native — works in SiS and with .streamlit/secrets.toml.
    2. Named/default connection in ~/.snowflake/connections.toml (developer fallback).
       Set the SNOWFLAKE_CONNECTION_NAME env var to select a specific named
       connection; if unset, the connector's default connection is used.
    After connecting, ensures an active warehouse is set (required for Cortex calls).
    The most recent failure reason is stashed in _LAST_CONN_ERROR so the UI can
    surface WHY a connection couldn't be made.
    """
    global _LAST_CONN_ERROR
    conn = None

    # 1. An explicitly-set SNOWFLAKE_CONNECTION_NAME wins — it is the user's
    #    deliberate choice and must override any default or secrets.toml.
    conn_name = os.environ.get("SNOWFLAKE_CONNECTION_NAME")
    if conn_name:
        try:
            import snowflake.connector
            conn = snowflake.connector.connect(connection_name=conn_name)
            _LAST_CONN_ERROR = ""
        except Exception as e:
            _LAST_CONN_ERROR = f"connect(connection_name={conn_name!r}) failed: {e}"

    # 2. Streamlit-native — used in Streamlit in Snowflake (SiS) and when a
    #    .streamlit/secrets.toml [connections.snowflake] block is present.
    if conn is None:
        try:
            conn = st.connection("snowflake")._instance
            if conn is not None:
                _LAST_CONN_ERROR = ""
        except Exception as e:
            _LAST_CONN_ERROR = f"st.connection('snowflake'): {e}"

    # 3. Connector's default connection (default_connection_name in config.toml).
    if conn is None:
        try:
            import snowflake.connector
            conn = snowflake.connector.connect()
            _LAST_CONN_ERROR = ""
        except Exception as e:
            _LAST_CONN_ERROR = ("no SNOWFLAKE_CONNECTION_NAME set and the default "
                                f"connection could not be opened: {e}")

    if conn is None:
        return None

    # Ensure an active warehouse — required even for serverless Cortex functions
    try:
        cur = conn.cursor()
        cur.execute("SELECT CURRENT_WAREHOUSE()")
        wh = cur.fetchone()[0]
        if not wh:
            cur.execute("SHOW WAREHOUSES")
            rows = cur.fetchall()
            if rows:
                cur.execute(f'USE WAREHOUSE "{rows[0][0]}"')
    except Exception:
        pass

    return conn


st.set_page_config(
    page_title="SnowPro Core Prep",
    page_icon="❄️",
    layout="wide",
)

SNOWFLAKE_BLUE = "#29B5E8"
SNOWFLAKE_DARK = "#11567F"
SNOWFLAKE_LIGHT = "#E3F5FC"
SNOWFLAKE_NAVY = "#0D2B45"
SNOWFLAKE_WHITE = "#FFFFFF"

DOMAINS = {
    "1": {"name": "Snowflake AI Data Cloud Features & Architecture", "weight": "31%", "icon": "☁️"},
    "2": {"name": "Account Management & Data Governance", "weight": "20%", "icon": "🛡️"},
    "3": {"name": "Data Loading, Unloading & Connectivity", "weight": "18%", "icon": "📤"},
    "4": {"name": "Performance Optimization, Querying & Transformation", "weight": "21%", "icon": "⚡"},
    "5": {"name": "Data Collaboration", "weight": "10%", "icon": "🤝"},
}

# ---------------------------------------------------------------------------
# CONCEPT_BANK — drives LLM question generation. ~80 entries, domain-weighted
# to match COF-C03 published weights.
# ---------------------------------------------------------------------------
CONCEPT_BANK = [
    # Domain 1 — Snowflake AI Data Cloud Features & Architecture (31%)
    {"concept": "Snowflake's three-layer architecture: cloud services, compute, and centralized storage", "domain": "1"},
    {"concept": "Snowflake micro-partition storage characteristics", "domain": "1"},
    {"concept": "Virtual warehouse sizing, credit consumption, and scaling behavior", "domain": "1"},
    {"concept": "Snowflake editions and the features each unlocks", "domain": "1"},
    {"concept": "Snowflake table types: permanent, transient, temporary, external, dynamic, and Iceberg", "domain": "1"},
    {"concept": "View types in Snowflake: standard, secure, materialized, and recursive", "domain": "1"},
    {"concept": "Cloud Services layer responsibilities and metadata management", "domain": "1"},
    {"concept": "Warehouse types: Standard vs Snowpark-Optimized", "domain": "1"},
    {"concept": "Cloud Services layer billing model", "domain": "1"},
    {"concept": "Snowflake client interfaces: Snowsight, SnowSQL, Snowflake CLI, and IDE extensions", "domain": "1"},
    {"concept": "Apache Iceberg table format in Snowflake", "domain": "1"},
    {"concept": "Snowpark: writing Python, Java, or Scala code that executes on Snowflake compute", "domain": "1"},
    {"concept": "Streamlit in Snowflake: building and deploying interactive data apps", "domain": "1"},
    {"concept": "Snowflake Notebooks for interactive SQL, Python, and Markdown development", "domain": "1"},
    {"concept": "Snowflake Cortex AI SQL functions", "domain": "1"},
    {"concept": "Cortex Search: hybrid search service", "domain": "1"},
    {"concept": "Cortex Analyst: natural language to SQL", "domain": "1"},
    {"concept": "Snowflake ML capabilities: forecasting, anomaly detection, and model registry", "domain": "1"},
    {"concept": "Snowflake serverless compute features", "domain": "1"},
    {"concept": "Semi-structured data types in Snowflake: VARIANT, OBJECT, ARRAY", "domain": "1"},
    {"concept": "Geospatial data types and Vector embeddings support in Snowflake", "domain": "1"},
    {"concept": "Sequences: auto-generating unique identifiers", "domain": "1"},
    {"concept": "URL types for accessing staged unstructured files", "domain": "1"},
    {"concept": "Snowflake connectivity: JDBC, ODBC, and language-specific connectors", "domain": "1"},
    {"concept": "External tables: querying data in cloud storage without loading it", "domain": "1"},
    {"concept": "Directory tables: cataloging files stored in stages", "domain": "1"},
    {"concept": "Query Profile and Query Insights in Snowsight", "domain": "1"},
    # Domain 2 — Account Management & Data Governance (20%)
    {"concept": "System-defined role hierarchy in Snowflake", "domain": "2"},
    {"concept": "Role-Based Access Control (RBAC) and Discretionary Access Control (DAC)", "domain": "2"},
    {"concept": "Snowflake authentication methods and multi-factor authentication", "domain": "2"},
    {"concept": "Network policies: controlling access by IP address", "domain": "2"},
    {"concept": "Dynamic Data Masking policies", "domain": "2"},
    {"concept": "Row Access Policies: row-level security", "domain": "2"},
    {"concept": "Projection Policies: controlling column visibility in query results", "domain": "2"},
    {"concept": "Object tagging and tag-based governance", "domain": "2"},
    {"concept": "Automated data classification and PII detection", "domain": "2"},
    {"concept": "Resource Monitors: credit usage controls", "domain": "2"},
    {"concept": "ACCOUNT_USAGE vs INFORMATION_SCHEMA: differences in retention, latency, and scope", "domain": "2"},
    {"concept": "Accessing the SNOWFLAKE system database and ACCOUNT_USAGE views", "domain": "2"},
    {"concept": "Snowflake encryption at rest and in transit, including Tri-Secret Secure", "domain": "2"},
    {"concept": "Trust Center: security posture, compliance benchmarks, and threat detection", "domain": "2"},
    {"concept": "Privacy policies: aggregation constraints and differential privacy", "domain": "2"},
    {"concept": "Data lineage and access history tracking", "domain": "2"},
    # Domain 3 — Data Loading, Unloading & Connectivity (18%)
    {"concept": "COPY INTO command: batch loading from stage into Snowflake tables", "domain": "3"},
    {"concept": "COPY INTO for data unloading: exporting data from tables to stages with file format and compression options", "domain": "3"},
    {"concept": "LIST and VALIDATE commands for stage file management and load validation", "domain": "3"},
    {"concept": "Snowflake stage types: user, table, named internal, and named external stages", "domain": "3"},
    {"concept": "Snowpipe: continuous file-based serverless data ingestion", "domain": "3"},
    {"concept": "Snowpipe Streaming: row-level low-latency ingestion using the Ingest SDK", "domain": "3"},
    {"concept": "Recommended file sizing and parallelism for data loading", "domain": "3"},
    {"concept": "Semi-structured file formats supported for loading: JSON, Avro, ORC, Parquet, XML", "domain": "3"},
    {"concept": "FLATTEN function for expanding nested semi-structured data into rows", "domain": "3"},
    {"concept": "PUT and GET commands for staging files", "domain": "3"},
    {"concept": "Snowflake Streams for change data capture", "domain": "3"},
    {"concept": "Snowflake Tasks for scheduled and triggered SQL execution", "domain": "3"},
    {"concept": "Dynamic Tables: declarative pipeline tables with automatic refresh", "domain": "3"},
    {"concept": "Storage Integrations for secure access to external cloud storage", "domain": "3"},
    {"concept": "Git integration in Snowflake", "domain": "3"},
    {"concept": "ON_ERROR handling options in COPY INTO", "domain": "3"},
    # Domain 4 — Performance Optimization, Querying & Transformation (21%)
    {"concept": "Snowflake's three caching layers and how each works", "domain": "4"},
    {"concept": "Result cache behavior: when it applies and what invalidates it", "domain": "4"},
    {"concept": "Scaling up vs scaling out: warehouse size vs multi-cluster", "domain": "4"},
    {"concept": "Multi-cluster warehouse scaling policies", "domain": "4"},
    {"concept": "Warehouse resize behavior during active query execution", "domain": "4"},
    {"concept": "Clustering keys and their role in micro-partition pruning", "domain": "4"},
    {"concept": "Automatic Clustering service", "domain": "4"},
    {"concept": "Search Optimization Service: accelerating point lookups and substring searches", "domain": "4"},
    {"concept": "Query Acceleration Service: serverless compute for large table scans", "domain": "4"},
    {"concept": "Materialized Views: precomputed, auto-maintained query results", "domain": "4"},
    {"concept": "Micro-partition pruning and partition statistics", "domain": "4"},
    {"concept": "Query spilling to disk: causes and remediation", "domain": "4"},
    {"concept": "UDFs and UDTFs: scalar user-defined functions vs table-returning user-defined table functions", "domain": "4"},
    {"concept": "Data sampling in Snowflake: BERNOULLI, SYSTEM, and TABLESAMPLE methods", "domain": "4"},
    {"concept": "SQL window functions in Snowflake", "domain": "4"},
    {"concept": "LATERAL FLATTEN for querying semi-structured array data", "domain": "4"},
    {"concept": "Workload isolation using separate virtual warehouses", "domain": "4"},
    {"concept": "Query queuing and concurrency management", "domain": "4"},
    {"concept": "Snowpark-Optimized warehouse characteristics and use cases", "domain": "4"},
    # Domain 5 — Data Collaboration (10%)
    {"concept": "Time Travel: querying and restoring historical data", "domain": "5"},
    {"concept": "Fail-safe: last-resort data recovery after Time Travel expiry", "domain": "5"},
    {"concept": "Time Travel retention and Fail-safe availability by table type", "domain": "5"},
    {"concept": "Zero-Copy Cloning: instant metadata-based copy with copy-on-write", "domain": "5"},
    {"concept": "Secure Data Sharing: live read-only data access without copying", "domain": "5"},
    {"concept": "Database replication and failover capabilities across Snowflake editions", "domain": "5"},
    {"concept": "Snowflake Marketplace: data products, listings, and Native Apps", "domain": "5"},
    {"concept": "Snowflake Native Apps Framework: packaging and distributing applications", "domain": "5"},

    # --- Additional coverage (appended AFTER the original 86 so existing
    #     cache indices 0-85 stay aligned). Grouped by domain via the
    #     "domain" field, not by position. ---
    # Domain 1
    {"concept": "Stored procedures: procedural logic in SQL, Python, JavaScript, Java, or Scala with caller's vs owner's rights", "domain": "1"},
    {"concept": "Databases and schemas: logical object containers, managed access schemas, and fully-qualified naming", "domain": "1"},
    {"concept": "Snowflake pricing and cost model: credits, on-demand vs capacity, storage billing, and cloud regions", "domain": "1"},
    # Domain 2
    {"concept": "SSO and federated authentication using SAML 2.0 with an external identity provider", "domain": "2"},
    {"concept": "OAuth and key-pair authentication for programmatic and service-account access", "domain": "2"},
    {"concept": "Privileges, grants, object ownership, and future grants in the RBAC model", "domain": "2"},
    {"concept": "Account and session parameters: levels, hierarchy, and precedence", "domain": "2"},
    # Domain 3
    {"concept": "File Format objects: named, reusable parse definitions and their options", "domain": "3"},
    {"concept": "Snowflake Kafka connector for streaming ingestion into tables", "domain": "3"},
    # Domain 4
    {"concept": "Core SQL transformations: MERGE, multi-table INSERT, and CREATE TABLE AS SELECT (CTAS)", "domain": "4"},
    {"concept": "Transactions in Snowflake: COMMIT, ROLLBACK, and autocommit behavior", "domain": "4"},
    {"concept": "Cardinality estimation functions: APPROX_COUNT_DISTINCT (HyperLogLog) and approximate aggregation", "domain": "4"},
    # Domain 5
    {"concept": "Reader accounts: sharing data with non-Snowflake consumers", "domain": "5"},
    {"concept": "Data Exchange: private, curated data hubs for a group of accounts", "domain": "5"},
    {"concept": "Direct shares vs Marketplace listings: differences, discovery, and use cases", "domain": "5"},
    {"concept": "Data Clean Rooms: privacy-preserving multi-party data collaboration", "domain": "5"},
]

QUESTION_BANK = [{'id': 1, 'domain': '1', 'type': 'single', 'question': "Which of the following is NOT one of the three main layers of Snowflake's architecture?", 'options': ['Presentation Layer', 'Compute Layer (Virtual Warehouses)', 'Storage Layer', 'Cloud Services Layer'], 'answer': [0], 'explanation': "Snowflake's architecture consists of three layers: Cloud Services (brain), Compute (muscle/virtual warehouses), and Storage (centralized, columnar). There is no 'Presentation Layer' in Snowflake's architecture."}, {'id': 2, 'domain': '1', 'type': 'single', 'question': 'What type of storage architecture does Snowflake use?', 'options': ['Shared-nothing with independent compute and storage nodes', 'Hybrid of shared-disk and shared-nothing', 'Shared-disk with a single centralized storage layer', 'Shared-everything with tightly coupled compute and storage'], 'answer': [1], 'explanation': 'Snowflake uses a hybrid architecture combining shared-disk (centralized storage accessible by all compute nodes) and shared-nothing (each virtual warehouse has its own compute resources and caching). This is a key differentiator.'}, {'id': 3, 'domain': '1', 'type': 'single', 'question': 'In Snowflake, data is stored in which format?', 'options': ['Columnar micro-partitions', 'JSON documents stored in row-based format per record', 'Row-based format with traditional heap table storage', 'Parquet files using external columnar compression'], 'answer': [0], 'explanation': 'Snowflake stores data in compressed, columnar micro-partitions. Each micro-partition is between 50-500 MB of uncompressed data, automatically organized. This enables efficient pruning and query performance.'}, {'id': 4, 'domain': '1', 'type': 'single', 'question': 'Which Snowflake edition is the minimum required to use multi-cluster warehouses?', 'options': ['Standard', 'Enterprise', 'Virtual Private Snowflake (VPS)', 'Business Critical'], 'answer': [1], 'explanation': 'Multi-cluster warehouses require Enterprise edition or higher. Standard edition only supports single-cluster warehouses. This is commonly tested on the exam.'}, {'id': 5, 'domain': '1', 'type': 'single', 'question': "Which layer of Snowflake's architecture handles authentication, query parsing, and metadata management?", 'options': ['Compute Layer handling authentication and metadata', 'Network Layer managing query optimization and security', 'Cloud Services Layer', 'Storage Layer coordinating metadata and authentication'], 'answer': [2], 'explanation': "The Cloud Services Layer is the 'brain' of Snowflake. It handles authentication, access control, infrastructure management, metadata, query parsing/optimization, and transaction management."}, {'id': 6, 'domain': '1', 'type': 'multi', 'question': 'Which of the following are characteristics of Snowflake micro-partitions? (Select TWO)', 'options': ['They are immutable once written', 'They can be manually reorganized by the user', 'They contain 50-500 MB of uncompressed data', 'They use row-based storage'], 'answer': [0, 2], 'explanation': 'Micro-partitions are immutable (write-once, never modified in place) and contain 50-500 MB of uncompressed data. They are automatically managed by Snowflake (not manually reorganized) and use columnar storage, not row-based.'}, {'id': 7, 'domain': '1', 'type': 'single', 'question': 'What is the purpose of the result cache in Snowflake?', 'options': ['To return results of previously executed queries without re-running them', 'To store raw data files from the storage layer', 'To pre-compute and store aggregation results for all tables on a nightly schedule', 'To persist intermediate query results between sessions so users can resume long-running queries'], 'answer': [0], 'explanation': "The result cache (held in the Cloud Services Layer) stores results of queries for 24 hours. If the same query is re-executed and the underlying data hasn't changed, results are returned instantly at no compute cost."}, {'id': 8, 'domain': '1', 'type': 'single', 'question': 'How does Snowflake handle storage and compute scaling?', 'options': ['Storage and compute are tightly coupled and must be scaled together as a single unit', 'Only storage can be scaled; compute is fixed', 'Compute resources scale automatically based on data volume, and storage scales with warehouse size', 'Storage and compute scale independently'], 'answer': [3], 'explanation': 'A fundamental Snowflake concept: storage and compute scale independently. You can increase storage without adding compute, and vice versa. This separation is a core architectural advantage over traditional data warehouses.'}, {'id': 9, 'domain': '1', 'type': 'single', 'question': 'Which cloud platforms does Snowflake currently run on?', 'options': ['AWS only, with plans to expand to other providers', 'AWS, Azure, and Google Cloud Platform', 'AWS, Azure, Google Cloud, Oracle Cloud, and IBM Cloud', 'AWS and Azure only, with Google Cloud in private preview'], 'answer': [1], 'explanation': 'Snowflake runs on AWS, Microsoft Azure, and Google Cloud Platform. It is cloud-agnostic and available across multiple regions on all three major cloud providers.'}, {'id': 10, 'domain': '1', 'type': 'single', 'question': 'What is a Snowflake virtual warehouse?', 'options': ['A type of database schema that organizes tables logically', 'A named abstraction for a cluster of compute resources', 'A network security boundary controlling inbound traffic', 'A physical data center location managed by Snowflake directly'], 'answer': [1], 'explanation': 'A virtual warehouse is a named abstraction for a cluster of compute resources (CPU, memory, temp storage). Virtual warehouses perform query execution and DML operations. They can be started, stopped, and resized independently.'}, {'id': 11, 'domain': '1', 'type': 'multi', 'question': 'Which of the following Snowflake editions support account failover and failback for business continuity? (Select TWO)', 'options': ['Business Critical', 'Enterprise', 'Standard', 'Virtual Private Snowflake'], 'answer': [0, 3], 'explanation': 'Failover/failback for business continuity requires Business Critical or VPS. Note: database and share REPLICATION is available on ALL editions, but failover/failback (promoting a secondary to primary) requires Business Critical+.'}, {'id': 12, 'domain': '1', 'type': 'single', 'question': 'Which Snowflake feature provides automatic clustering of data within micro-partitions?', 'options': ['Automatic Clustering', 'Search Optimization Service', 'Materialized Views', 'Query Acceleration Service'], 'answer': [0], 'explanation': 'Automatic Clustering is a Snowflake service (Enterprise+) that automatically reorganizes data in micro-partitions based on a defined clustering key to improve query performance by optimizing data pruning.'}, {'id': 13, 'domain': '2', 'type': 'single', 'question': 'Which system role in Snowflake is the top-level administrator and can manage all aspects of the account?', 'options': ['ORGADMIN', 'SYSADMIN', 'PUBLIC', 'ACCOUNTADMIN'], 'answer': [3], 'explanation': 'ACCOUNTADMIN is the top-level role combining SYSADMIN and SECURITYADMIN. It should be used sparingly and with MFA enabled. It can manage all objects, users, roles, billing, and account-level settings.'}, {'id': 14, 'domain': '2', 'type': 'single', 'question': "In Snowflake's access control model, who owns an object by default?", 'options': ['The role that created the object', 'SYSADMIN, which manages all objects in the account', 'ACCOUNTADMIN, the top-level administrative role', 'The user who ran the CREATE statement for the object'], 'answer': [0], 'explanation': "In Snowflake, the role active at the time of object creation becomes the owner of that object. Ownership is tied to the role, not the individual user. This is fundamental to Snowflake's RBAC model."}, {'id': 15, 'domain': '2', 'type': 'single', 'question': 'Which Snowflake role is specifically designed to create and manage users and roles?', 'options': ['SYSADMIN', 'ACCOUNTADMIN', 'SECURITYADMIN', 'USERADMIN'], 'answer': [3], 'explanation': 'USERADMIN is specifically designed for creating and managing users and roles. SECURITYADMIN can manage grants on objects and inherits USERADMIN. SYSADMIN manages warehouses and databases.'}, {'id': 16, 'domain': '2', 'type': 'multi', 'question': 'Which of the following are valid primary authentication methods in Snowflake? (Select THREE)', 'options': ['SAML-based SSO', 'Multi-factor authentication (MFA)', 'Username/password', 'Key pair authentication', 'Biometric authentication'], 'answer': [0, 2, 3], 'explanation': 'Snowflake supports three primary authentication methods: username/password, key pair authentication, and SAML-based SSO (federated authentication). MFA is a second factor added on top of password auth (using Passkeys, TOTP authenticator apps, or Duo), not a standalone primary method. Snowflake does not support biometric auth directly.'}, {'id': 17, 'domain': '2', 'type': 'single', 'question': 'What does the SECURITYADMIN role primarily manage?', 'options': ['Creating and managing virtual warehouses and databases', 'Object-level grants and security policies', 'Managing organization-level settings and accounts across the Snowflake organization', 'Creating and configuring Resource Monitors to control warehouse credit usage'], 'answer': [1], 'explanation': 'SECURITYADMIN manages object-level grants, inherits USERADMIN (user/role management), and manages security policies. SYSADMIN manages warehouses/databases. ACCOUNTADMIN handles billing.'}, {'id': 18, 'domain': '2', 'type': 'single', 'question': 'In Snowflake, what is a Resource Monitor used for?', 'options': ['Tracking the number of concurrent users', 'Managing database replication across regions', 'Controlling and monitoring credit usage', 'Enforcing row-level security on sensitive data'], 'answer': [2], 'explanation': 'Resource Monitors control and monitor credit usage by virtual warehouses. They can be set at the account or warehouse level with actions like notify, suspend, or immediately suspend when thresholds are reached.'}, {'id': 19, 'domain': '2', 'type': 'single', 'question': 'Which system-defined role should be used to create warehouses and databases?', 'options': ['SYSADMIN', 'SECURITYADMIN', 'USERADMIN', 'PUBLIC'], 'answer': [0], 'explanation': 'SYSADMIN is the recommended role for creating and managing warehouses and databases. While ACCOUNTADMIN can also do this, best practice is to use SYSADMIN for object management and reserve ACCOUNTADMIN for account-level administration.'}, {'id': 20, 'domain': '2', 'type': 'single', 'question': 'Which statement best describes the default system role hierarchy in Snowflake?', 'options': ['SECURITYADMIN is the top-level role with ACCOUNTADMIN reporting to it directly', 'SYSADMIN is the top-level role that inherits from all other roles including ACCOUNTADMIN', 'ACCOUNTADMIN > SECURITYADMIN > SYSADMIN > USERADMIN > PUBLIC in a single linear chain with no branching', 'ACCOUNTADMIN inherits both SYSADMIN and SECURITYADMIN; SECURITYADMIN inherits USERADMIN; PUBLIC is the base role'], 'answer': [3], 'explanation': 'The hierarchy branches: ACCOUNTADMIN inherits both SYSADMIN and SECURITYADMIN. SECURITYADMIN inherits USERADMIN. SYSADMIN manages objects (warehouses, databases). All custom roles should be granted to SYSADMIN (best practice). PUBLIC is the base role granted to every user. ORGADMIN operates at the organization level above individual accounts.'}, {'id': 21, 'domain': '3', 'type': 'single', 'question': 'Which Snowflake feature provides continuous, serverless data loading from files staged in cloud storage?', 'options': ['Snowpipe', 'Tasks', 'External Tables', 'Data Replication'], 'answer': [0], 'explanation': 'Snowpipe provides continuous, serverless data loading. It automatically detects new files in a stage (via cloud notifications or REST API calls) and loads them within minutes. Unlike COPY INTO, it uses serverless compute.'}, {'id': 22, 'domain': '3', 'type': 'single', 'question': 'What is the default file format for the COPY INTO command in Snowflake?', 'options': ['CSV', 'JSON', 'Parquet', 'AVRO'], 'answer': [0], 'explanation': 'The default file format for COPY INTO is CSV (delimited). You can specify other formats like JSON, Avro, ORC, Parquet, or XML using the FILE_FORMAT option.'}, {'id': 23, 'domain': '3', 'type': 'multi', 'question': 'Which of the following are valid Snowflake stage types? (Select FOUR)', 'options': ['Table stage (@%table_name)', 'User stage (@~)', 'Named internal stage', 'Named external stage', 'Temporary stage'], 'answer': [0, 1, 2, 3], 'explanation': "Snowflake has User stages (@~, one per user), Table stages (@%table_name, one per table), and Named stages — which can be either internal (Snowflake-managed storage) or external (customer-managed cloud storage like S3/Azure/GCS). All four are valid. There is no 'Temporary stage' type."}, {'id': 24, 'domain': '3', 'type': 'single', 'question': 'Which command is used to upload files from a local file system to an internal Snowflake stage?', 'options': ['INSERT INTO', 'PUT', 'COPY INTO', 'LOAD DATA'], 'answer': [1], 'explanation': 'PUT is used to upload (stage) files from a local file system to an internal Snowflake stage. COPY INTO is then used to load staged data into tables. PUT can only be run from SnowSQL or connectors that support it.'}, {'id': 25, 'domain': '3', 'type': 'single', 'question': 'What is the maximum recommended file size for data loading into Snowflake?', 'options': ['10 MB per micro-partition before compression is applied', '100 MB per micro-partition with automatic row-level compression', '1 GB per micro-partition stored in columnar format', '100-250 MB compressed'], 'answer': [3], 'explanation': 'Snowflake recommends files be 100-250 MB compressed for optimal parallel loading. Files that are too small create overhead; files that are too large limit parallelism. Snowflake can split large files if the format supports it.'}, {'id': 26, 'domain': '3', 'type': 'single', 'question': 'Which semi-structured data format is NOT natively supported by Snowflake?', 'options': ['JSON', 'YAML', 'Avro', 'Parquet'], 'answer': [1], 'explanation': 'Snowflake natively supports JSON, Avro, ORC, Parquet, and XML for semi-structured data. YAML is not a supported file format for data loading.'}, {'id': 27, 'domain': '3', 'type': 'single', 'question': 'What data type is used to store semi-structured data in Snowflake?', 'options': ['VARIANT', 'STRING', 'OBJECT', 'JSON'], 'answer': [0], 'explanation': 'VARIANT is the primary data type for semi-structured data in Snowflake. It can store JSON, Avro, XML, etc. up to 16 MB compressed. OBJECT and ARRAY are also semi-structured types but VARIANT is the most general.'}, {'id': 28, 'domain': '3', 'type': 'single', 'question': 'What does the VALIDATION_MODE parameter do in a COPY INTO statement?', 'options': ['Returns validation results without loading any data', 'Validates that the file format matches the target table schema before loading begins', 'Automatically corrects formatting errors in the data files during the validation pass', 'Enforces strict schema matching and rejects files where column count differs from the target table'], 'answer': [0], 'explanation': 'VALIDATION_MODE allows you to validate staged data files without actually loading them. Options include RETURN_n_ROWS, RETURN_ERRORS, and RETURN_ALL_ERRORS. This is useful for testing data quality before loading.'}, {'id': 29, 'domain': '3', 'type': 'single', 'question': 'Which Snowflake feature allows you to query data in external cloud storage without loading it into Snowflake tables?', 'options': ['Data Sharing', 'Materialized Views', 'Snowpipe', 'External Tables'], 'answer': [3], 'explanation': 'External Tables allow querying data in cloud storage (S3, Azure Blob, GCS) without loading it into Snowflake. The data stays in external storage and is read at query time. This is useful for data lake integration.'}, {'id': 30, 'domain': '4', 'type': 'single', 'question': 'Which of the following is NOT a type of caching in Snowflake?', 'options': ['Result cache', 'Warehouse cache', 'Index cache', 'Metadata cache'], 'answer': [2], 'explanation': "Snowflake uses three types of caching: Result Cache (24h, Cloud Services Layer), Local Disk Cache (warehouse SSD cache for micro-partitions), and Metadata Cache (Cloud Services Layer for table metadata). There is no 'Index cache' as Snowflake does not use traditional indexes."}, {'id': 31, 'domain': '4', 'type': 'single', 'question': 'What is the effect of scaling UP a virtual warehouse (e.g., from X-Small to Large)?', 'options': ['Increases the number of concurrent queries the warehouse can handle simultaneously', 'Reduces credit consumption by optimizing resource allocation per query', 'Provides more compute resources per query for faster execution', 'Increases available storage capacity for the warehouse cache'], 'answer': [2], 'explanation': 'Scaling UP (increasing warehouse size) provides more compute resources per query, making complex queries run faster. Scaling OUT (multi-cluster) handles more concurrent queries. Scaling up doubles resources and credits with each size increase.'}, {'id': 32, 'domain': '4', 'type': 'single', 'question': 'What is the purpose of a clustering key in Snowflake?', 'options': ['To define the sort order of data within micro-partitions for better pruning', 'To create a B-tree index structure for faster point lookups on specific columns', 'To create a primary key constraint that enforces uniqueness across rows', 'To partition data across multiple warehouses for parallel query processing'], 'answer': [0], 'explanation': 'A clustering key defines how data is organized within micro-partitions. Well-clustered data enables better partition pruning, which reduces the amount of data scanned during queries. This is NOT an index and NOT a primary key.'}, {'id': 33, 'domain': '4', 'type': 'single', 'question': "In a multi-cluster warehouse, what does the 'scaling policy' determine?", 'options': ['When to start and shut down additional clusters', 'The per-minute credit consumption rate for each active cluster', 'The maximum warehouse size that each cluster can scale to', 'Which specific queries get priority in the execution queue'], 'answer': [0], 'explanation': 'The scaling policy (Standard or Economy) determines when additional clusters start/stop. Standard starts clusters immediately when queries queue. Economy waits 6 minutes before starting additional clusters to conserve credits.'}, {'id': 34, 'domain': '4', 'type': 'single', 'question': 'What does the QUERY_HISTORY function/view help you analyze?', 'options': ['Data loading history including COPY INTO statistics and file metrics', 'User login history with timestamps, IP addresses, and auth methods', 'Query execution details including status, duration, and resources used', 'Only failed queries and error messages from the past 24 hours with no performance metrics'], 'answer': [2], 'explanation': 'QUERY_HISTORY (function and ACCOUNT_USAGE view) shows detailed query execution information including status, start/end time, bytes scanned, rows produced, warehouse used, and more. Essential for performance analysis.'}, {'id': 35, 'domain': '4', 'type': 'single', 'question': 'What happens when a virtual warehouse is suspended in Snowflake?', 'options': ['All cached data is retained in local SSD storage until the warehouse is resized', 'Running queries are terminated immediately and must be resubmitted', 'No credits are consumed, but local cache is eventually lost', 'The warehouse is permanently deleted and must be recreated from scratch'], 'answer': [2], 'explanation': 'When suspended, a warehouse stops consuming credits. Running queries complete first (unless force-suspended). The local disk cache may persist for some time but is eventually lost. The warehouse can be resumed at any time.'}, {'id': 36, 'domain': '4', 'type': 'single', 'question': 'What is partition pruning in Snowflake?', 'options': ['Splitting large micro-partitions into smaller units for parallel scanning', 'Compressing micro-partition metadata to reduce Cloud Services overhead', 'Manually deleting old micro-partitions to reclaim storage space', 'The optimizer skipping micro-partitions that cannot contain relevant data'], 'answer': [3], 'explanation': "Partition pruning is Snowflake's process of using metadata (min/max values, distinct count, null count) for each micro-partition to skip partitions that cannot contain data matching the query filter. This significantly reduces data scanned."}, {'id': 37, 'domain': '4', 'type': 'multi', 'question': 'Which of the following factors affect virtual warehouse credit consumption? (Select TWO)', 'options': ['Number of databases in the account', 'Warehouse size (T-shirt sizing)', 'Amount of data stored', 'Duration the warehouse is running'], 'answer': [1, 3], 'explanation': 'Credit consumption depends on warehouse size (each size up doubles credits/hour) and how long it runs. Storage is billed separately. Number of databases does not affect warehouse billing.'}, {'id': 38, 'domain': '5', 'type': 'single', 'question': 'What is the default Time Travel retention period for permanent tables in Snowflake?', 'options': ['1 day', '7 days', '14 days', '0 days'], 'answer': [0], 'explanation': 'The default Time Travel retention period is 1 day (24 hours) for all table types. Enterprise edition and above can extend this up to 90 days for permanent tables. Transient and temporary tables have a maximum of 1 day.'}, {'id': 39, 'domain': '5', 'type': 'single', 'question': 'What is Fail-safe in Snowflake?', 'options': ['A backup system managed by the user through scheduled COPY INTO operations', 'An alternative to Time Travel that provides the same functionality for transient tables', 'A 7-day period after Time Travel expires where Snowflake can recover data (Snowflake-initiated only)', 'A disaster recovery feature for automatic multi-region replication of all databases'], 'answer': [2], 'explanation': "Fail-safe is a 7-day period after Time Travel expires. Data recovery during Fail-safe can ONLY be done by Snowflake support (not self-service). It's a last resort for data recovery. Transient and temporary tables do NOT have Fail-safe."}, {'id': 40, 'domain': '5', 'type': 'single', 'question': 'Which SQL command allows you to query historical data using Time Travel?', 'options': ["SELECT ... REVERT TO (TIMESTAMP => 'value') for point-in-time recovery", "SELECT ... RECOVER FROM (TIMESTAMP => 'value') with automatic rollback", 'SELECT ... FROM ... AT(TIMESTAMP => ...)', "SELECT ... HISTORY FROM (TIMESTAMP => 'value') for historical snapshots"], 'answer': [2], 'explanation': "Time Travel uses AT or BEFORE clauses: SELECT * FROM table AT(TIMESTAMP => '...') or AT(OFFSET => -300) or BEFORE(STATEMENT => 'query_id'). This lets you query data as it existed at a past point in time."}, {'id': 41, 'domain': '5', 'type': 'single', 'question': 'What is the purpose of the UNDROP command in Snowflake?', 'options': ['To recover data from the Fail-safe period, which requires a support ticket', 'To undo the last DML statement executed against a table automatically', 'To permanently delete a table and its associated Time Travel data', 'To restore a dropped table, schema, or database within the Time Travel retention period'], 'answer': [3], 'explanation': "UNDROP restores a dropped table, schema, or database within the Time Travel retention period. It's a powerful recovery tool. After the Time Travel period expires, the object can only be recovered through Fail-safe (by Snowflake support)."}, {'id': 42, 'domain': '5', 'type': 'single', 'question': 'Which table type has Fail-safe protection in Snowflake?', 'options': ['Permanent tables', 'Transient tables', 'Temporary tables', 'External tables'], 'answer': [0], 'explanation': 'Only permanent tables have Fail-safe protection (7 days after Time Travel expires). Transient tables, temporary tables, and external tables do NOT have Fail-safe. This reduces their storage costs but means data cannot be recovered by Snowflake support after Time Travel expires.'}, {'id': 43, 'domain': '5', 'type': 'single', 'question': 'What is Zero-Copy Cloning in Snowflake?', 'options': ['Creating a metadata-only copy that references the original micro-partitions until data is modified', 'A method of backing up databases to external cloud storage for disaster recovery', 'Copying data between different Snowflake accounts using secure data sharing', 'Creating a full physical copy of all data in a table using storage duplication'], 'answer': [0], 'explanation': 'Zero-Copy Cloning creates a metadata-only snapshot referencing the same underlying micro-partitions. No data is physically copied until modifications are made (copy-on-write). This makes cloning nearly instantaneous and cost-effective.'}, {'id': 44, 'domain': '5', 'type': 'multi', 'question': 'Which of the following objects can be cloned in Snowflake? (Select THREE)', 'options': ['Databases', 'Warehouses', 'Shares', 'Schemas', 'Tables'], 'answer': [0, 3, 4], 'explanation': 'Databases, schemas, tables, streams, sequences, file formats, tasks, and stages can be cloned. Shares and warehouses CANNOT be cloned. Cloning a database or schema clones all contained objects.'}, {'id': 45, 'domain': '2', 'type': 'single', 'question': 'Which Snowflake feature automatically encrypts all data at rest and in transit?', 'options': ['Dynamic Data Masking', 'End-to-end encryption (always-on)', 'External Tokenization', 'Row Access Policies'], 'answer': [1], 'explanation': 'Snowflake provides automatic end-to-end encryption: AES-256 for data at rest and TLS 1.2+ for data in transit. This is always on and requires no configuration. Business Critical adds customer-managed key support (Tri-Secret Secure).'}, {'id': 46, 'domain': '5', 'type': 'single', 'question': 'What is Snowflake Secure Data Sharing?', 'options': ['Encryption at rest uses customer-managed keys by default across all Snowflake editions', 'A VPN connection between Snowflake accounts', 'Sharing live, read-only data between accounts without copying or moving data', 'Encryption is optional and must be explicitly enabled by the account administrator'], 'answer': [2], 'explanation': 'Secure Data Sharing enables sharing live, read-only access to data between Snowflake accounts without any data copying or movement. The data provider controls access; consumers query data in place. No ETL needed.'}, {'id': 47, 'domain': '5', 'type': 'single', 'question': 'What is a Snowflake Data Share?', 'options': ['A replicated database automatically synchronized across multiple regions', 'A materialized view that automatically replicates data across multiple Snowflake accounts', 'A compressed file export of database tables written to an external stage', 'A named object containing grants to databases, schemas, tables, and views shared with consumers'], 'answer': [3], 'explanation': 'A Share is a named Snowflake object that encapsulates all information needed for sharing: grants on shared databases/schemas/tables/views. Providers create shares, add objects, and grant access to consumer accounts.'}, {'id': 48, 'domain': '5', 'type': 'single', 'question': 'Which entity pays for the compute costs when a data consumer queries shared data?', 'options': ['The data provider', 'Costs are split equally', 'The data consumer', 'Snowflake pays for shared data queries'], 'answer': [2], 'explanation': 'The data consumer pays for compute costs (their own virtual warehouse) when querying shared data. The provider only pays for storage. This is a key point for the exam - there is no data transfer or replication cost.'}, {'id': 49, 'domain': '5', 'type': 'single', 'question': 'What is the Snowflake Marketplace?', 'options': ['A private data exchange where only accounts within the same organization can share datasets', 'A repository for Snowflake documentation, guides, and release notes', 'A platform where providers can publish and consumers can discover/access shared data products', 'A repository of sample databases and demo datasets included with every new Snowflake account'], 'answer': [2], 'explanation': 'The Snowflake Marketplace is where data providers publish data products (datasets, data services) and consumers discover and access them. It enables monetization of data and instant access without ETL.'}, {'id': 50, 'domain': '5', 'type': 'single', 'question': 'Can Snowflake data be shared with non-Snowflake users?', 'options': ["Yes, through direct database links that expose the provider's account", 'Yes, but only through exporting data as files and sharing externally', 'No, both parties must have their own full Snowflake accounts to share data', 'Yes, through Reader Accounts provisioned by the data provider'], 'answer': [3], 'explanation': 'Reader Accounts allow providers to share data with non-Snowflake users. The provider creates and manages the Reader Account and pays for all compute costs the reader incurs. Reader Accounts have limited functionality compared to full Snowflake accounts.'}, {'id': 51, 'domain': '2', 'type': 'single', 'question': 'Which Snowflake view provides credit consumption history for warehouses over the last 365 days?', 'options': ['ACCOUNT_USAGE.QUERY_HISTORY', 'ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY', 'ACCOUNT_USAGE.STORAGE_USAGE', 'ACCOUNT_USAGE.LOGIN_HISTORY'], 'answer': [1], 'explanation': 'ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY provides 365 days of credit consumption data. INFORMATION_SCHEMA version is limited to 14 days. ACCOUNT_USAGE views have a latency of 45 minutes to 3 hours.'}, {'id': 52, 'domain': '2', 'type': 'single', 'question': 'What is the key difference between INFORMATION_SCHEMA and ACCOUNT_USAGE views?', 'options': ['ACCOUNT_USAGE only stores data for the current session and resets when the user logs out', 'INFORMATION_SCHEMA has longer data retention, up to 365 days of historical query data', 'ACCOUNT_USAGE has longer retention (up to 365 days) but with latency; INFORMATION_SCHEMA is real-time but shorter retention (7-14 days)', 'They contain identical data with no differences in latency, retention, or access patterns'], 'answer': [2], 'explanation': 'ACCOUNT_USAGE (in SNOWFLAKE database) retains data up to 365 days but has 45min-3hr latency. INFORMATION_SCHEMA provides real-time data but only 7-14 days retention. ACCOUNT_USAGE requires IMPORTED PRIVILEGES grant on SNOWFLAKE database.'}, {'id': 53, 'domain': '1', 'type': 'single', 'question': 'What does the AUTO_SUSPEND parameter control for a virtual warehouse?', 'options': ['The number of seconds a warehouse waits for new queries before scaling down cluster count', 'The number of seconds of inactivity before the warehouse is automatically suspended', 'The time in seconds before cached query results expire and must be recomputed', 'The maximum query execution time in seconds before a query is automatically cancelled'], 'answer': [1], 'explanation': 'AUTO_SUSPEND specifies the number of seconds of inactivity after which a warehouse is automatically suspended. Common values: 60 (1 min), 300 (5 min), 600 (10 min). Setting to 0 or NULL prevents auto-suspend.'}, {'id': 54, 'domain': '2', 'type': 'single', 'question': 'Which Snowflake feature allows you to set up alerts based on specific conditions in your data?', 'options': ['Snowflake Alerts', 'Resource Monitors', 'Streams', 'Tasks'], 'answer': [0], 'explanation': 'Snowflake Alerts (CREATE ALERT) allow you to define conditions and trigger notifications (email) or actions when those conditions are met. Resource Monitors only track credit usage. Tasks schedule SQL execution.'}, {'id': 55, 'domain': '5', 'type': 'single', 'question': 'What is the maximum Time Travel retention period for Snowflake Enterprise edition?', 'options': ['1 day', '45 days', '90 days', '365 days'], 'answer': [2], 'explanation': 'Enterprise edition supports up to 90 days of Time Travel for permanent tables. Standard edition is limited to 0 or 1 day. Transient and temporary tables are always limited to 0 or 1 day regardless of edition.'}, {'id': 56, 'domain': '1', 'type': 'single', 'question': 'What is Snowpark?', 'options': ['A developer framework for writing code in Python, Java, or Scala that executes in Snowflake', 'A data visualization tool for creating charts and dashboards in Snowflake', 'A network security feature for managing private connectivity to Snowflake', "Snowflake's mobile application for monitoring account activity and usage"], 'answer': [0], 'explanation': "Snowpark is Snowflake's developer framework allowing you to write data processing code in Python, Java, or Scala that executes directly in Snowflake. It includes DataFrame API, UDFs, and stored procedures."}, {'id': 57, 'domain': '3', 'type': 'single', 'question': 'What is a Snowflake Stream used for?', 'options': ['Logging warehouse activity including query counts and resource utilization metrics', 'Capturing change data (CDC) - tracking inserts, updates, and deletes on a table', 'Real-time data streaming from Kafka topics directly into Snowflake tables', 'Streaming query results to external systems via outbound REST API calls'], 'answer': [1], 'explanation': 'Streams record change data capture (CDC) information on a table: inserts, updates, and deletes. They track the delta of changes since the last consumption. Streams are commonly paired with Tasks for ETL pipelines.'}, {'id': 58, 'domain': '3', 'type': 'single', 'question': 'What is a Snowflake Task?', 'options': ['A type of virtual warehouse optimized for batch processing workloads', 'A user permission that controls access to scheduled query execution', 'A scheduled SQL statement or stored procedure that can run on a recurring basis', 'A one-time SQL execution that runs immediately and logs results in the query history'], 'answer': [2], 'explanation': 'A Task is a Snowflake object that schedules SQL statements or stored procedures. Tasks can run on a cron schedule or fixed interval. They can be chained in DAGs (directed acyclic graphs) for complex pipelines.'}, {'id': 59, 'domain': '1', 'type': 'single', 'question': 'Which Snowflake feature allows you to build interactive data applications using Python?', 'options': ['Snowpark', 'Snowpipe', 'Data Sharing', 'Streamlit in Snowflake'], 'answer': [3], 'explanation': 'Streamlit in Snowflake (SiS) enables building interactive data applications using Python directly within Snowflake. It provides UI widgets, charts, and data display components without needing HTML/CSS/JS.'}, {'id': 60, 'domain': '1', 'type': 'single', 'question': 'What is Snowflake Cortex?', 'options': ['A data governance framework for managing access policies and compliance', 'A warehouse scheduling system for automated start and stop operations', 'A network security layer managing VPN and private connectivity options', 'A suite of AI/ML functions and services built into Snowflake'], 'answer': [3], 'explanation': 'Snowflake Cortex provides AI/ML capabilities within Snowflake, including LLM functions (COMPLETE, SUMMARIZE, TRANSLATE, SENTIMENT, etc.), ML-based forecasting, anomaly detection, and classification without moving data.'}, {'id': 61, 'domain': '1', 'type': 'single', 'question': 'What is the minimum Snowflake edition required for Tri-Secret Secure (customer-managed encryption keys)?', 'options': ['Standard', 'Enterprise', 'Virtual Private Snowflake (VPS)', 'Business Critical'], 'answer': [3], 'explanation': 'Tri-Secret Secure requires Business Critical edition or higher. It combines a Snowflake-maintained key with a customer-managed key (via cloud KMS) to create a composite encryption key. This provides customer control over data encryption.'}, {'id': 62, 'domain': '1', 'type': 'single', 'question': 'Which Snowflake feature provides a web-based interface for managing Snowflake resources, writing queries, and viewing results?', 'options': ['Classic Console', 'SnowSQL', 'Snowflake Worksheets', 'Snowsight'], 'answer': [3], 'explanation': "Snowsight is Snowflake's web-based user interface. It provides SQL worksheets, dashboards, data previewing, query profiling, account management, and collaboration features. SnowSQL is the command-line client."}, {'id': 63, 'domain': '3', 'type': 'single', 'question': 'What happens if the ON_ERROR option is set to CONTINUE in a COPY INTO statement?', 'options': ['Rows with errors are skipped, and loading continues with remaining rows', 'Error rows are loaded with NULL values replacing the invalid field data', 'The entire file is rejected and no rows from that file are committed', 'The load stops at the first error and rolls back all previously loaded rows'], 'answer': [0], 'explanation': 'ON_ERROR = CONTINUE skips rows that produce errors and continues loading the remaining rows. Other options: ABORT_STATEMENT (default, stops on first error), SKIP_FILE (skips the entire file with errors), SKIP_FILE_n (skips file after n errors).'}, {'id': 64, 'domain': '3', 'type': 'single', 'question': 'Which command unloads data from a Snowflake table to a stage?', 'options': ['GET @stage/file.csv', 'COPY INTO @stage', 'EXPORT INTO @stage FROM table', 'PUT file:///path @stage'], 'answer': [1], 'explanation': 'COPY INTO @stage_name FROM table_name unloads data from a table to a stage (internal or external). The GET command then downloads files from an internal stage to a local file system. There is no EXPORT command.'}, {'id': 65, 'domain': '4', 'type': 'single', 'question': 'What is the Snowflake Query Profile used for?', 'options': ['Analyzing query execution plans, identifying bottlenecks, and understanding data flow', 'Reviewing query history with a focus on cost attribution and credit usage per query', 'Viewing table statistics including row counts, storage size, and clustering depth', 'Displaying real-time data previews of query results before full execution completes'], 'answer': [0], 'explanation': "The Query Profile in Snowsight provides a visual execution plan showing operator nodes, data flow, partition pruning stats, spillage to disk, and performance bottlenecks. It's essential for query optimization."}, {'id': 66, 'domain': '4', 'type': 'single', 'question': "When does data 'spill to disk' in Snowflake, and why is it a concern?", 'options': ['When the result cache is full and older entries are evicted — it is automatically handled', 'When data is written to permanent storage during normal DML operations — expected behavior', 'When intermediate query results exceed available memory and must use local/remote disk - it degrades performance', 'When a query reads more micro-partitions than expected due to poor clustering on the filter columns'], 'answer': [2], 'explanation': 'Spillage occurs when intermediate results exceed memory. Data spills first to local SSD (faster) then to remote storage (slower). Seeing spillage in the Query Profile suggests the warehouse may need to be scaled up.'}, {'id': 67, 'domain': '2', 'type': 'single', 'question': 'What is a Dynamic Data Masking policy in Snowflake?', 'options': ["A column-level security feature that masks data at query time based on the user's role", 'A policy that automatically deletes sensitive data after a specified retention period', 'A policy that encrypts data at the storage level using customer-managed encryption keys', 'A policy that prevents data from being shared externally through listings or direct shares'], 'answer': [0], 'explanation': "Dynamic Data Masking applies masking rules to columns at query time based on the querying user's role. For example, showing full SSN to ADMIN but '***-**-1234' to ANALYST. It requires Enterprise edition or higher."}, {'id': 68, 'domain': '2', 'type': 'single', 'question': 'What is a Row Access Policy in Snowflake?', 'options': ["A policy that controls row insertion permissions based on the executing user's role", 'A policy that limits the number of rows returned by a query to prevent data exfiltration', 'A policy that controls which rows a user can see based on their role or attributes', 'A policy that encrypts specific rows using column-level encryption with per-row keys'], 'answer': [2], 'explanation': 'Row Access Policies filter rows returned to users based on their role, department, or other attributes. For example, a regional manager only sees data for their region. Requires Enterprise edition or higher.'}, {'id': 69, 'domain': '5', 'type': 'single', 'question': 'What is the relationship between Time Travel and Fail-safe in terms of storage costs?', 'options': ['Only Time Travel incurs additional storage costs; Fail-safe storage is included free', 'Time Travel and Fail-safe storage are free and do not count toward the storage bill', 'Both Time Travel and Fail-safe incur additional storage costs for maintaining historical data', 'Only Fail-safe incurs additional storage costs; Time Travel uses the active data storage'], 'answer': [2], 'explanation': 'Both Time Travel and Fail-safe incur additional storage costs. Time Travel retains historical versions of data for the retention period. Fail-safe retains data for 7 additional days. These costs are proportional to the amount of data changed.'}, {'id': 70, 'domain': '5', 'type': 'single', 'question': 'What types of objects can be included in a Snowflake Share?', 'options': ['Tables, external tables, secure views, secure materialized views, and secure UDFs', 'Tables and warehouses that are owned by the sharing role in the provider account', 'Only tables and their underlying micro-partitions are eligible for inclusion in shares', 'Any Snowflake object including warehouses, roles, users, and network policies'], 'answer': [0], 'explanation': 'Shares can include tables, external tables, secure views, secure materialized views, and secure UDFs. Secure views are commonly used to control exactly which data consumers see. Regular (non-secure) views cannot be shared.'}, {'id': 71, 'domain': '1', 'type': 'single', 'question': 'What is the primary purpose of the Cloud Services Layer in Snowflake?', 'options': ['Storing data in micro-partitions and managing the physical storage layout on disk', 'Processing and executing all SQL queries submitted by users across all active sessions', 'Executing SQL queries by allocating compute resources from the warehouse pool', 'Coordinating activities across Snowflake including authentication, query optimization, and metadata management'], 'answer': [3], 'explanation': "The Cloud Services Layer coordinates all activities: authentication, access control, query parsing/optimization, metadata management, infrastructure management, and transaction management. It's the 'brain' of Snowflake."}, {'id': 72, 'domain': '4', 'type': 'single', 'question': 'What is the FLATTEN function used for in Snowflake?', 'options': ['Removing duplicate rows from a table using automatic deduplication logic', 'Compressing semi-structured data for more efficient storage in VARIANT columns', 'Converting semi-structured (nested) data into a relational (tabular) format', 'Flattening the warehouse scaling curve by distributing queries across clusters'], 'answer': [2], 'explanation': 'FLATTEN is a table function that takes a VARIANT, OBJECT, or ARRAY column and produces a lateral view (rows) from the nested data. Essential for querying JSON arrays and nested structures in a relational format.'}, {'id': 73, 'domain': '4', 'type': 'single', 'question': 'What is the difference between Standard and Economy scaling policies for multi-cluster warehouses?', 'options': ['Standard scaling uses significantly more credits per cluster than Economy scaling mode', 'Economy scaling mode delays adding clusters for up to 6 minutes to conserve credits', 'Standard adds clusters immediately when queries queue; Economy waits up to 6 minutes to conserve credits', 'Both policies behave identically with the same cluster startup timing and shutdown rules'], 'answer': [2], 'explanation': 'Standard scaling adds clusters immediately when queries start queuing, prioritizing performance. Economy waits up to 6 minutes before starting additional clusters, prioritizing credit conservation over immediate performance.'}, {'id': 74, 'domain': '4', 'type': 'single', 'question': 'How can you view the query execution plan in Snowflake?', 'options': ['Querying the INFORMATION_SCHEMA.EXECUTION_PLANS view for historical plan statistics', 'Using EXPLAIN command or viewing the Query Profile in Snowsight', 'Using the SHOW PLANS command to display stored execution plans for recent queries', 'Running DESCRIBE TABLE to view the column statistics used by the query optimizer'], 'answer': [1], 'explanation': 'You can use the EXPLAIN command to see a textual execution plan or view the Query Profile in Snowsight for a visual, interactive execution plan with detailed statistics about each operator.'}, {'id': 75, 'domain': '2', 'type': 'single', 'question': 'What is a Network Policy in Snowflake?', 'options': ['A policy that controls inter-warehouse communication and resource sharing between clusters', 'A policy that controls virtual warehouse network bandwidth and query throughput limits', 'A policy that restricts access to Snowflake based on IP addresses (allowed/blocked lists)', 'A policy that manages data replication traffic across regions and cloud providers'], 'answer': [2], 'explanation': 'Network Policies restrict access to Snowflake based on IP addresses using allowed and blocked lists. They can be applied at the account level or to individual users. This provides an additional layer of security.'}, {'id': 76, 'domain': '1', 'type': 'single', 'question': 'How many credits per hour does an X-Small warehouse consume?', 'options': ['0.5 credits/hour', '2 credits/hour', '4 credits/hour', '1 credit/hour'], 'answer': [3], 'explanation': 'An X-Small warehouse consumes 1 credit/hour. Each size increase doubles the credits: Small=2, Medium=4, Large=8, X-Large=16, 2X-Large=32, 3X-Large=64, 4X-Large=128, 5X-Large=256, 6X-Large=512 credits/hour.'}, {'id': 77, 'domain': '3', 'type': 'single', 'question': 'What is the purpose of a File Format object in Snowflake?', 'options': ['To define the physical storage format of tables including compression and encoding options', 'To define the format of log files generated by warehouse operations and query execution', 'To define how data files are parsed during loading and unloading (e.g., delimiters, compression, encoding)', 'To format query output for display in Snowsight including column widths and date formats'], 'answer': [2], 'explanation': 'A File Format object is a named, reusable definition of how to parse data files. It specifies type (CSV, JSON, etc.), compression, delimiters, encoding, error handling, and more. It simplifies COPY INTO statements.'}, {'id': 78, 'domain': '1', 'type': 'single', 'question': 'Which of the following statements about Transient tables is correct?', 'options': ['Transient tables have 7 days of Fail-safe protection to allow recovery of accidentally dropped data', 'Transient tables are automatically deleted after 24 hours of inactivity', 'Transient tables have 0 or 1 day Time Travel and no Fail-safe, reducing storage costs', 'Transient tables support up to 90 days of Time Travel retention on Enterprise edition and above'], 'answer': [2], 'explanation': "Transient tables have 0 or 1 day of Time Travel and NO Fail-safe. This makes them cheaper for staging/ETL data where data protection is less critical. They persist until explicitly dropped (they're not automatically deleted)."}, {'id': 79, 'domain': '1', 'type': 'single', 'question': 'What are User-Defined Functions (UDFs) in Snowflake?', 'options': ['Functions that manage user authentication and session tokens within Snowflake', 'Custom functions that can be written in SQL, JavaScript, Python, Java, or Scala and called in SQL queries', 'Functions written exclusively in SQL that wrap existing built-in Snowflake expressions', 'Built-in Snowflake system functions provided as part of the SQL standard library'], 'answer': [1], 'explanation': "UDFs allow you to write custom functions in SQL, JavaScript, Python, Java, or Scala that can be called within SQL queries. They enable extending Snowflake's functionality with custom logic. UDTFs (table functions) return tabular results."}, {'id': 80, 'domain': '4', 'type': 'single', 'question': 'What is a Materialized View in Snowflake?', 'options': ['A temporary view created during query execution that exists only for the session duration', 'A precomputed view that stores results physically and is automatically maintained by Snowflake', 'A view that requires ACCOUNTADMIN privileges and stores encrypted results separately', 'A view that caches its results in the Cloud Services Layer for faster subsequent access'], 'answer': [1], 'explanation': 'Materialized views store precomputed results physically and Snowflake automatically maintains them as underlying data changes. They improve query performance for expensive computations but have additional storage and compute costs. Requires Enterprise edition.'}, {'id': 81, 'domain': '1', 'type': 'single', 'question': 'What database and schema are automatically available in every Snowflake account?', 'options': ['SYSTEM.METADATA schema containing all account-level monitoring and usage data', 'MASTER.DBO schema which stores all system configuration and metadata records', 'PUBLIC.DEFAULT schema where all account monitoring views are created automatically', 'SNOWFLAKE.ACCOUNT_USAGE and SNOWFLAKE.INFORMATION_SCHEMA'], 'answer': [3], 'explanation': 'The SNOWFLAKE database is a system-provided, read-only shared database available in every account. It contains ACCOUNT_USAGE, ORGANIZATION_USAGE, and other schemas with views for monitoring and administration.'}, {'id': 82, 'domain': '2', 'type': 'single', 'question': 'What is the PUBLIC role in Snowflake?', 'options': ['A role that allows access to the Snowflake Marketplace for discovering data products', 'A role specifically for managing public data shares and listings in the Marketplace', 'A role automatically granted to every user in the account that provides minimal base privileges', 'A role that provides full administrative access to all objects and settings in the account'], 'answer': [2], 'explanation': 'PUBLIC is automatically granted to every user. It provides base-level privileges and is at the bottom of the role hierarchy. Any privileges granted to PUBLIC are available to all users in the account.'}, {'id': 83, 'domain': '3', 'type': 'single', 'question': 'What is the difference between an Internal Stage and an External Stage?', 'options': ['Internal stages are free of storage charges; external stages incur additional cloud fees', 'Both stage types function identically and use the same billing model for storage', 'Internal stages store data within Snowflake-managed storage; external stages reference data in customer-managed cloud storage (S3, Azure Blob, GCS)', 'Internal stages are faster for loading; external stages are consistently slower for all operations'], 'answer': [2], 'explanation': 'Internal stages store data in Snowflake-managed cloud storage. External stages are references to data in customer-managed cloud storage (AWS S3, Azure Blob Storage, GCS). External stages require storage integration or credentials.'}, {'id': 84, 'domain': '5', 'type': 'single', 'question': 'What is database replication in Snowflake?', 'options': ['Copying data from one table to another within the same account using CREATE TABLE AS SELECT', 'Backing up databases to external cloud storage locations using scheduled COPY INTO commands', 'Replicating databases across Snowflake accounts in different regions/cloud platforms for disaster recovery or data distribution', 'Creating materialized views of databases that automatically refresh on a defined schedule'], 'answer': [2], 'explanation': 'Database replication enables replicating databases across Snowflake accounts in different regions and cloud platforms. Replication is available on ALL editions. Failover/failback (promoting a secondary to primary) requires Business Critical edition or higher.'}, {'id': 85, 'domain': '1', 'type': 'single', 'question': 'What does the SHOW WAREHOUSES command display?', 'options': ['Warehouse properties including name, state, size, type, auto-suspend/resume settings, and credit usage', 'Only warehouses owned by the current active role, excluding inherited role grants', 'The SQL queries currently running on each warehouse with their execution duration', 'Only the names and sizes of warehouses with a simplified one-line-per-warehouse format'], 'answer': [0], 'explanation': "SHOW WAREHOUSES displays comprehensive warehouse information: name, state (Started/Suspended), size, type, auto-suspend, auto-resume, cluster count, scaling policy, owner, and more. Results depend on the user's role privileges."}, {'id': 86, 'domain': '4', 'type': 'single', 'question': 'What is the purpose of the LIMIT clause combined with result caching?', 'options': ['A LIMIT query can still benefit from result cache if the exact same query was run before', 'LIMIT clauses are not affected by caching and always trigger a full table scan', 'LIMIT automatically enables result caching regardless of the USE_CACHED_RESULT setting', 'LIMIT disables result caching because the output is considered non-deterministic'], 'answer': [0], 'explanation': "The result cache stores complete query results. If the exact same query (including LIMIT) was run within 24 hours and the underlying data hasn't changed, results are returned from cache without using compute."}, {'id': 87, 'domain': '1', 'type': 'multi', 'question': 'Which of the following are features exclusive to Business Critical edition? (Select TWO)', 'options': ['Tri-Secret Secure (customer-managed keys)', 'Automatic clustering', 'Database failover/failback', 'Multi-cluster warehouses', 'Time Travel up to 90 days'], 'answer': [0, 2], 'explanation': 'Tri-Secret Secure and database failover/failback are Business Critical features. Multi-cluster warehouses, 90-day Time Travel, and Automatic Clustering are Enterprise features. Understanding edition differences is commonly tested.'}, {'id': 88, 'domain': '3', 'type': 'single', 'question': 'What is Snowpipe Streaming?', 'options': ['Streaming query results to external systems via outbound REST API integration endpoints', 'A faster version of regular Snowpipe that processes files with reduced latency from stages', 'A low-latency data ingestion API that allows inserting rows directly into tables via the Snowflake Ingest SDK', 'A video streaming service within Snowflake for processing multimedia data in pipelines'], 'answer': [2], 'explanation': "Snowpipe Streaming uses the Snowflake Ingest SDK to write rows directly to Snowflake tables with low latency (sub-second). Unlike regular Snowpipe (file-based), it's designed for streaming data sources like Kafka."}, {'id': 89, 'domain': '5', 'type': 'single', 'question': 'What happens when you CREATE TABLE ... CLONE on a table with Time Travel data?', 'options': ['Only the most recent data snapshot is cloned, starting with a fresh Time Travel window', 'Cloning is not allowed on tables that have an active Time Travel retention policy', 'The Time Travel data is permanently deleted from the original table after the clone operation', 'The clone includes the table data and its Time Travel history at the time of cloning'], 'answer': [3], 'explanation': 'When you clone a table, the clone includes the current data AND the Time Travel data available at the time of cloning. You can even clone a table at a specific point in time using AT/BEFORE clauses.'}, {'id': 90, 'domain': '3', 'type': 'single', 'question': 'What is a Dynamic Table in Snowflake?', 'options': ['A temporary table that is automatically deleted when the current session ends', 'A declarative table that automatically materializes the results of a query with a target lag, replacing complex ETL pipelines', 'A table that automatically grows its allocated storage size based on insert volume', 'A table that dynamically changes its schema to accommodate new columns in loaded data'], 'answer': [1], 'explanation': 'Dynamic Tables are declaratively defined tables that automatically maintain their results based on a query. You specify a target lag, and Snowflake handles refresh scheduling. They simplify streaming/incremental data pipelines.'}, {'id': 91, 'domain': '2', 'type': 'single', 'question': 'What is a Tag in Snowflake used for?', 'options': ['A type of access control permission that restricts DML operations on tagged objects', 'A metadata object that can be assigned to Snowflake objects (columns, tables, etc.) for governance, tracking, and classification', 'A feature for tagging individual queries to track their performance in Query Profile', 'A method for labeling virtual warehouses to allocate and track credit consumption'], 'answer': [1], 'explanation': 'Tags are metadata objects (key-value pairs) that can be assigned to columns, tables, views, warehouses, and other objects. They support data governance, compliance tracking, sensitive data classification, and lineage.'}, {'id': 92, 'domain': '4', 'type': 'single', 'question': 'When using a warehouse, what determines the number of servers (nodes) in a cluster?', 'options': ['The amount of data stored in the database being queried', 'The total volume of data stored in the tables being queried by the warehouse', 'The number of queries currently queued in the warehouse execution queue', 'The warehouse size (T-shirt size: XS, S, M, L, etc.)'], 'answer': [3], 'explanation': 'The warehouse size determines the number of compute nodes. XS=1 node, S=2, M=4, L=8, XL=16, and so on (each doubling). More nodes means more compute resources per query and more credits per hour.'}, {'id': 93, 'domain': '1', 'type': 'single', 'question': 'What is the Snowflake Data Cloud?', 'options': ['A type of virtual warehouse optimized for data science and ML workloads', 'A specific cloud infrastructure provider that hosts Snowflake deployments', 'A data backup service that replicates databases across availability zones', "An ecosystem enabling data sharing, marketplace, and collaboration across organizations on Snowflake's platform"], 'answer': [3], 'explanation': 'The Snowflake Data Cloud is the ecosystem of Snowflake users, data providers, and consumers. It enables data sharing, the Marketplace, data applications, and collaboration across organizations, industries, and clouds.'}, {'id': 94, 'domain': '3', 'type': 'single', 'question': 'What is the GET command used for in Snowflake?', 'options': ['Downloading data from an external stage directly into a Snowflake table using COPY INTO', 'Downloading files from an internal Snowflake stage to a local file system', 'Retrieving query results from the result cache into a local client application', 'Getting metadata and statistics about a table using the DESCRIBE command'], 'answer': [1], 'explanation': "GET downloads files from an internal Snowflake stage to a local file system. It's the counterpart to PUT (which uploads local files to a stage). GET only works with internal stages, not external stages."}, {'id': 95, 'domain': '2', 'type': 'single', 'question': 'What is the purpose of the ACCESS_HISTORY view in ACCOUNT_USAGE?', 'options': ['Recording which columns and objects were accessed by queries, supporting compliance and auditing', 'Monitoring warehouse access patterns including login frequency and session duration', 'Tracking user login history only, without recording data access or query details', 'Logging API access to Snowflake including REST API calls and driver connections'], 'answer': [0], 'explanation': 'ACCESS_HISTORY tracks detailed access information: which queries accessed which columns and objects (both direct and base objects). This is valuable for compliance auditing, data governance, and understanding data usage patterns.'}, {'id': 96, 'domain': '2', 'type': 'single', 'question': 'How does Snowflake charge for cloud services layer usage?', 'options': ['Cloud services compute is always free and never billed regardless of usage volume', 'Cloud services are billed at the same rate as warehouse compute on a per-second basis', "A flat monthly fee is charged for cloud services based on the account's edition tier", 'Cloud services that exceed 10% of daily warehouse compute credits are billed'], 'answer': [3], 'explanation': 'Cloud services layer usage is free up to 10% of daily warehouse compute credit consumption. Only the amount exceeding 10% is billed. This means most customers pay nothing extra for cloud services.'}, {'id': 97, 'domain': '5', 'type': 'single', 'question': 'What is the AT keyword used for in Time Travel queries?', 'options': ['Specifying an exact offset in the transaction log to replay changes from that point forward', 'Defining a retention window that overrides the table-level DATA_RETENTION_TIME_IN_DAYS setting', 'Querying data as it existed at a specific point in time (inclusive of changes at that moment)', 'Specifying a future timestamp for scheduling deferred query execution at that time'], 'answer': [2], 'explanation': 'AT specifies an exact point in time and includes any changes at that moment. BEFORE specifies a point just before the given time/statement. Both support TIMESTAMP, OFFSET (seconds), and STATEMENT (query ID).'}, {'id': 98, 'domain': '5', 'type': 'single', 'question': 'Can shared data be modified by the consumer?', 'options': ['Yes, consumers can modify shared data using INSERT and UPDATE statements', 'No, shared data is read-only for consumers', 'Consumers can only modify data if granted write privileges by the provider', 'Shared data becomes editable after the consumer creates a local copy'], 'answer': [1], 'explanation': 'Shared data is strictly read-only for consumers. Consumers cannot modify, delete, or insert data in shared objects. They can only query the data. This is a fundamental security feature of Secure Data Sharing.'}, {'id': 99, 'domain': '1', 'type': 'single', 'question': 'What is a Stored Procedure in Snowflake?', 'options': ['A monitoring script that tracks warehouse utilization and sends alert notifications', 'A precompiled query stored in the result cache that executes without re-optimization', 'A type of materialized view that refreshes incrementally on a defined schedule', 'A named block of code (SQL, JavaScript, Python, Java, or Scala) that can be called to perform operations including DDL and DML'], 'answer': [3], 'explanation': "Stored Procedures are named blocks of code that can perform DDL/DML operations, control flow, and complex logic. They can be written in SQL, JavaScript, Python, Java, or Scala. They run with either caller's or owner's rights."}, {'id': 100, 'domain': '2', 'type': 'single', 'question': 'What is the ORGADMIN role used for?', 'options': ['Managing organization-level operations across multiple Snowflake accounts', 'Managing external data shares and listings across accounts in the Marketplace', 'Administering the Snowflake Marketplace including provider onboarding and reviews', "Managing a single Snowflake account's users, roles, and security settings"], 'answer': [0], 'explanation': 'ORGADMIN manages organization-level operations: creating/managing accounts, viewing organization usage, enabling replication across accounts, and managing organization-level settings. It operates above individual accounts.'}, {'id': 101, 'domain': '1', 'type': 'single', 'question': 'What are the main components stored in the Cloud Services Layer metadata?', 'options': ['Table metadata, micro-partition statistics (min/max, distinct count, null count), query history, and access control information', 'Raw data stored in JSON format within the Cloud Services Layer memory cache', 'Only table names and column data types from the information schema catalog', 'Only user credentials and authentication tokens for session management'], 'answer': [0], 'explanation': 'The Cloud Services Layer stores rich metadata: table/column info, micro-partition statistics (for pruning), user/role info, query history, warehouse config, transaction info, and access control metadata.'}, {'id': 102, 'domain': '4', 'type': 'single', 'question': 'What is the recommended approach when queries are running slowly due to high concurrency?', 'options': ['Increase the Time Travel retention period', 'Add more clustering keys', 'Scale UP the warehouse (bigger size)', 'Scale OUT using multi-cluster warehouses'], 'answer': [3], 'explanation': 'For concurrency issues (many queries queueing), scale OUT with multi-cluster warehouses. For individual query performance, scale UP to a larger size. This distinction is frequently tested on the exam.'}, {'id': 103, 'domain': '3', 'type': 'single', 'question': 'What is the purpose of a Storage Integration in Snowflake?', 'options': ['A Snowflake object that stores credentials and configuration for accessing external cloud storage, avoiding embedded credentials', 'Managing internal stage storage limits including quota allocation per user or role', 'Integrating storage capacity across multiple Snowflake accounts in an organization', 'Integrating Snowflake with third-party BI tools like Tableau, Looker, and Power BI'], 'answer': [0], 'explanation': 'Storage Integrations store the configuration (IAM role, service principal, etc.) for accessing external cloud storage. They avoid embedding credentials in SQL statements and provide a secure, reusable way to access S3, Azure Blob, or GCS.'}, {'id': 104, 'domain': '2', 'type': 'single', 'question': 'What is the SNOWFLAKE.ORGANIZATION_USAGE schema used for?', 'options': ['Configuring organization-level billing settings and payment methods for all accounts', 'Managing organization-level security policies including network rules and SSO settings', 'Viewing aggregate usage across all accounts in an organization', 'Viewing usage metrics and query history for a single individual Snowflake account'], 'answer': [2], 'explanation': 'ORGANIZATION_USAGE provides views for aggregate usage across all accounts in a Snowflake organization: total credits, storage, data transfer, and more. Requires ORGADMIN role or appropriate grants.'}, {'id': 105, 'domain': '3', 'type': 'single', 'question': 'What are Snowflake Connectors used for?', 'options': ['Providing pre-built integrations to load data from external sources like Kafka, Spark, and Python into Snowflake', 'Creating direct network links between Snowflake accounts for low-latency data transfer', 'Connecting databases across regions to enable cross-region JOIN queries natively', 'Linking user accounts to external identity providers for federated authentication'], 'answer': [0], 'explanation': 'Snowflake Connectors (Kafka connector, Spark connector, Python connector, etc.) provide pre-built integrations for loading data from external systems. They simplify data ingestion from popular data platforms and programming languages.'}, {'id': 106, 'domain': '1', 'type': 'single', 'question': 'What is a Virtual Private Snowflake (VPS)?', 'options': ['A VPN connection to Snowflake that encrypts all traffic between client and server', 'An enterprise security add-on that enables private connectivity via AWS PrivateLink or Azure Private Link', 'A type of virtual warehouse with dedicated compute resources and guaranteed SLA', 'The highest level of isolation with a completely separate Snowflake deployment and dedicated infrastructure'], 'answer': [3], 'explanation': "VPS provides the highest level of security and isolation. It uses a completely separate Snowflake deployment with dedicated infrastructure (no shared resources). It's designed for organizations with the strictest security requirements."}, {'id': 107, 'domain': '2', 'type': 'single', 'question': 'What is federated authentication in Snowflake?', 'options': ['Federating data access across multiple Snowflake regions using cross-region replication', 'A multi-factor authentication method that uses multiple identity verification steps', 'Using a single password across all Snowflake accounts within an organization for simplicity', 'Integrating Snowflake with an external identity provider (IdP) using SAML 2.0 for single sign-on (SSO)'], 'answer': [3], 'explanation': 'Federated authentication integrates Snowflake with an external identity provider (like Okta, Azure AD, etc.) using SAML 2.0 for SSO. Users authenticate through the IdP rather than Snowflake directly.'}, {'id': 108, 'domain': '3', 'type': 'single', 'question': 'What does the STRIP_OUTER_ARRAY option do when loading JSON data?', 'options': ['Strips leading and trailing whitespace from all values within the array elements', 'Removes all arrays from the JSON document and converts them to scalar NULL values', 'Removes the outer array brackets so each element becomes a separate row', 'Converts arrays to concatenated strings using a comma delimiter between elements'], 'answer': [2], 'explanation': 'STRIP_OUTER_ARRAY = TRUE removes the outer array brackets [ ] from JSON data during loading, so each element in the array becomes a separate row in the table. This is useful when JSON files contain an array of records.'}, {'id': 109, 'domain': '1', 'type': 'single', 'question': 'What is the difference between a Temporary table and a Transient table?', 'options': ['Temporary tables have Fail-safe protection; Transient tables do not have Fail-safe', 'Both table types behave identically with the same Time Travel, Fail-safe, and visibility rules', 'Temporary tables exist only within the session and are invisible to other sessions; Transient tables persist across sessions but lack Fail-safe', 'Transient tables are session-scoped and invisible to other sessions; Temporary tables persist'], 'answer': [2], 'explanation': 'Temporary tables are session-scoped (dropped at session end) and invisible to other sessions/users. Transient tables persist until explicitly dropped and are visible to other users with access. Neither has Fail-safe; both have 0-1 day Time Travel.'}, {'id': 110, 'domain': '4', 'type': 'single', 'question': 'What is the SEARCH OPTIMIZATION SERVICE in Snowflake?', 'options': ['A tool for optimizing LIKE queries in WHERE clauses by rewriting them as regex patterns', 'A full-text search engine that indexes all string columns in a database automatically', 'A background service that creates search access paths to improve point lookup and substring search performance on large tables', 'An optimization that caches search results in the Cloud Services Layer for fast retrieval'], 'answer': [2], 'explanation': 'The Search Optimization Service creates search access paths that significantly improve point lookup queries (equality predicates) and substring/regex searches on large tables. It requires Enterprise edition and has additional costs.'}, {'id': 111, 'domain': '1', 'type': 'single', 'question': 'What happens to running queries when a warehouse is resized?', 'options': ['Running queries are paused mid-execution and restarted with the new compute resources', 'All running queries are immediately cancelled and must be resubmitted by the users', 'Running queries complete with current resources; new queries use the updated size', 'The warehouse must be fully suspended before any size changes can be applied'], 'answer': [2], 'explanation': 'When you resize a running warehouse, currently executing queries complete with the existing resources. Only new queries submitted after the resize will use the updated warehouse size. No disruption to running workloads.'}, {'id': 112, 'domain': '5', 'type': 'single', 'question': 'What is a Data Exchange in Snowflake?', 'options': ['An API for exchanging data with external systems via REST endpoints and webhooks', 'A data replication tool for synchronizing databases between different cloud regions', 'A private, curated hub for a group of accounts to share and discover data, separate from the public Marketplace', 'A mechanism for converting data between formats such as JSON, CSV, Parquet, and Avro'], 'answer': [2], 'explanation': "A Data Exchange is a private hub where a selected group of Snowflake accounts can publish, discover, and share data. Unlike the public Marketplace, it's curated and limited to invited members. Useful for industry-specific data sharing."}, {'id': 113, 'domain': '2', 'type': 'single', 'question': 'What is the purpose of the WAREHOUSE_LOAD_HISTORY view?', 'options': ['Listing all warehouses in the account with their current size and state information', 'Showing data loading statistics for COPY INTO commands including rows loaded and errors', 'Displaying warehouse billing history with daily credit consumption breakdowns per warehouse', 'Showing the average and peak workload on warehouse clusters over time (running, queued, blocked queries)'], 'answer': [3], 'explanation': 'WAREHOUSE_LOAD_HISTORY (in ACCOUNT_USAGE) shows warehouse utilization: average running, queued, and blocked query loads in 5-minute intervals. It helps analyze workload patterns. Note: for data loading history, use LOAD_HISTORY or COPY_HISTORY views instead.'}, {'id': 114, 'domain': '3', 'type': 'single', 'question': 'What is the Snowflake Kafka Connector used for?', 'options': ['Replicating Snowflake data to Kafka brokers for downstream event-driven processing', 'Streaming data from Snowflake tables to Kafka topics for real-time change data capture', 'Continuously loading data from Kafka topics into Snowflake tables using Snowpipe or Snowpipe Streaming', 'Managing Kafka cluster configurations including broker settings and topic partitioning'], 'answer': [2], 'explanation': 'The Snowflake Kafka Connector reads from Kafka topics and loads data into Snowflake tables. It can use either Snowpipe (file-based) or Snowpipe Streaming (row-based) for ingestion, supporting both batch and real-time patterns.'}, {'id': 115, 'domain': '2', 'type': 'single', 'question': 'Which of the following are valid second factors of authentication (MFA methods) in Snowflake?', 'options': ['Only hardware security keys using the FIDO2/WebAuthn standard as dedicated physical devices', 'Passkeys, TOTP authenticator apps (e.g., Google Authenticator), and Duo', 'Only Duo Mobile push notifications as the exclusive supported MFA second factor', 'Only biometric authentication methods such as fingerprint and facial recognition scanning'], 'answer': [1], 'explanation': 'Snowflake supports three MFA second-factor methods: (1) Passkeys (WebAuthn-based, recommended), (2) TOTP authenticator apps like Google Authenticator or Microsoft Authenticator, and (3) Duo. Administrators can control which methods are available via authentication policies. MFA is strongly recommended for ACCOUNTADMIN and can be enforced at the account level.'}, {'id': 116, 'domain': '1', 'type': 'single', 'question': "What does the term 'zero-maintenance' mean in the context of Snowflake?", 'options': ['Snowflake automatically handles infrastructure provisioning, tuning, optimization, data distribution, and software updates with no DBA intervention needed', 'Snowflake never has any downtime, including during major version upgrades and patch releases', 'Users never need to write SQL queries because Snowflake auto-generates all data transformations', "Users don't need to manage their data models, schemas, or access controls in any way"], 'answer': [0], 'explanation': 'Zero-maintenance means Snowflake handles infrastructure, updates, optimization, data distribution, and maintenance automatically. No indexing, tuning, partitioning, or capacity planning by DBAs. Users focus on data and queries.'}, {'id': 117, 'domain': '4', 'type': 'single', 'question': 'What is the default maximum number of clusters for a multi-cluster warehouse?', 'options': ['3 clusters', '1 cluster', '10 clusters', '5 clusters'], 'answer': [2], 'explanation': 'The default maximum clusters is 10. When creating a multi-cluster warehouse, you specify MIN_CLUSTER_COUNT and MAX_CLUSTER_COUNT. Snowflake auto-scales between these based on the scaling policy (Standard or Economy). Enterprise edition is required.'}, {'id': 118, 'domain': '3', 'type': 'multi', 'question': 'Which of the following semi-structured data formats are supported by Snowflake? (Select THREE)', 'options': ['CSV', 'JSON', 'XML', 'YAML', 'Parquet'], 'answer': [1, 2, 4], 'explanation': 'Snowflake supports JSON, Avro, ORC, Parquet, and XML as semi-structured formats. YAML is not supported. CSV is a structured (delimited) format, not semi-structured. Parquet and ORC are columnar semi-structured formats.'}, {'id': 119, 'domain': '5', 'type': 'single', 'question': 'What is the maximum Time Travel retention period for transient tables?', 'options': ['7 days', '1 day', '90 days', '0 days'], 'answer': [1], 'explanation': 'Transient tables have a maximum Time Travel retention of 1 day (0 or 1), regardless of the Snowflake edition. Only permanent tables in Enterprise edition and above can have up to 90 days of Time Travel.'}, {'id': 120, 'domain': '5', 'type': 'single', 'question': 'What is a Listing in the Snowflake Marketplace?', 'options': ['A published data product (free or paid) that consumers can discover and access in the Marketplace', 'A list of available warehouses showing their sizes, states, and auto-suspend configurations', 'A database catalog entry that documents table schemas, column descriptions, and lineage', "A job posting on Snowflake's careers page listing open engineering and sales positions"], 'answer': [0], 'explanation': 'A Listing is a published data product in the Snowflake Marketplace. Providers create Listings to share datasets, data services, or Snowflake Native Apps. Listings can be free or paid, and are instantly accessible to consumers.'}, {'id': 121, 'domain': '1', 'type': 'single', 'question': 'What is an Apache Iceberg table in Snowflake?', 'options': ['A table type exclusive to the VPS (Virtual Private Snowflake) edition of Snowflake', 'An open table format that allows interoperability with other engines while being managed by Snowflake', 'A temporary table used for caching intermediate results during complex query execution', "A table that stores data exclusively in Snowflake's proprietary columnar micro-partition format"], 'answer': [1], 'explanation': 'Apache Iceberg tables in Snowflake use the open Iceberg table format, enabling interoperability with other compute engines (Spark, Flink, etc.) while Snowflake manages the table lifecycle, compaction, and optimization.'}, {'id': 122, 'domain': '1', 'type': 'single', 'question': 'What are Snowflake Notebooks?', 'options': ['A documentation tool for creating data dictionaries and column-level descriptions in Snowsight', 'A logging system for tracking query history, warehouse utilization, and account activity', 'A third-party integration that connects Jupyter notebooks to Snowflake for data analysis', 'An interactive, cell-based development environment built into Snowsight for SQL, Python, and Markdown'], 'answer': [3], 'explanation': 'Snowflake Notebooks are a native, interactive development environment in Snowsight. They support SQL, Python, and Markdown cells, and run on Snowflake compute. They are used for data exploration, ML development, and collaborative analysis.'}, {'id': 123, 'domain': '1', 'type': 'single', 'question': 'Which Snowflake Cortex function would you use to generate text responses from a large language model?', 'options': ['CORTEX.SUMMARIZE', 'CORTEX.COMPLETE', 'CORTEX.SENTIMENT', 'CORTEX.CLASSIFY'], 'answer': [1], 'explanation': 'CORTEX.COMPLETE is the function for generating text responses using large language models (LLMs). You provide a prompt and model name, and it returns generated text. Other functions have specific purposes: SUMMARIZE for summarization, SENTIMENT for sentiment scoring, CLASSIFY for classification.'}, {'id': 124, 'domain': '1', 'type': 'multi', 'question': 'Which of the following are Snowflake Cortex AI SQL functions? (Select THREE)', 'options': ['CORTEX.REPLICATE', 'CORTEX.CLASSIFY', 'CORTEX.BACKUP', 'CORTEX.COMPLETE', 'CORTEX.SUMMARIZE'], 'answer': [1, 3, 4], 'explanation': 'Cortex AI SQL functions include COMPLETE (LLM text generation), SUMMARIZE (text summarization), CLASSIFY (text classification), EXTRACT (entity extraction), SENTIMENT (sentiment analysis), and TRANSLATE (translation). REPLICATE and BACKUP are not Cortex functions.'}, {'id': 125, 'domain': '1', 'type': 'single', 'question': 'What is Streamlit in Snowflake (SiS)?', 'options': ['A Python-based framework for building interactive data applications that run natively inside Snowflake', 'A Python-based ETL framework for building and orchestrating data pipelines within Snowflake', 'A BI reporting tool for creating visualizations and dashboards from Snowflake data', 'A data loading utility for ingesting files from external stages into Snowflake tables'], 'answer': [0], 'explanation': "Streamlit in Snowflake allows developers to build interactive data applications using Python that run entirely within Snowflake's secure environment. Data never leaves Snowflake, and apps inherit Snowflake's RBAC."}, {'id': 126, 'domain': '1', 'type': 'single', 'question': 'Where does Snowpark code execute when running on Snowflake?', 'options': ['All Snowpark code executes on the client machine and sends only results to Snowflake', 'Directly on Snowflake virtual warehouse compute nodes (server-side)', 'Snowpark code runs on a separate serverless compute pool managed independently from warehouses', 'Snowpark code executes on a dedicated external compute cluster managed by the customer'], 'answer': [1], 'explanation': "Snowpark code (Python, Java, Scala) executes directly on Snowflake's virtual warehouse compute nodes via pushdown execution. The code is sent to Snowflake and runs server-side, so data never leaves the platform. This is different from client-side drivers that fetch data to the client."}, {'id': 127, 'domain': '1', 'type': 'single', 'question': 'What is the Snowflake CLI (SnowCLI)?', 'options': ['A GUI-based database management tool for visual schema design and query building', 'The same tool as SnowSQL, just rebranded with a different name and installer', 'A connector library for JDBC and ODBC access to Snowflake from external applications', 'A command-line tool for managing Snowflake objects, Streamlit apps, Snowpark projects, and Native Apps from the terminal'], 'answer': [3], 'explanation': 'The Snowflake CLI (snow) is a modern command-line tool for managing Snowflake resources including Streamlit apps, Snowpark projects, Native Apps, notebooks, and more. It is distinct from SnowSQL, which is primarily for SQL execution.'}, {'id': 128, 'domain': '3', 'type': 'single', 'question': 'What does the TARGET_LAG parameter control in a Dynamic Table?', 'options': ['The minimum interval between consecutive scheduled data loads into the dynamic table', 'The maximum allowable staleness of the data before Snowflake triggers a refresh', 'The maximum number of rows that can be refreshed in a single incremental refresh cycle', 'The time zone used for scheduling dynamic table refreshes relative to the account default'], 'answer': [1], 'explanation': "TARGET_LAG specifies the maximum acceptable data freshness. Snowflake automatically determines the refresh schedule to keep the dynamic table within this lag. For example, TARGET_LAG = '10 minutes' means Snowflake ensures data is never more than 10 minutes stale. You can also set it to DOWNSTREAM to inherit from downstream consumers."}, {'id': 129, 'domain': '3', 'type': 'single', 'question': 'What is a key technical difference between Snowpipe and Snowpipe Streaming?', 'options': ['Snowpipe Streaming is a managed upgrade path that replaces regular Snowpipe automatically', 'Snowpipe requires files staged first; Snowpipe Streaming inserts rows directly via the Ingest SDK without staging files', 'Snowpipe uses the Ingest SDK while Snowpipe Streaming processes staged files in batches', 'Regular Snowpipe provides lower latency because it skips file staging and validation steps'], 'answer': [1], 'explanation': 'The key difference is the ingestion method: Snowpipe is file-based (files must be staged first, then loaded), while Snowpipe Streaming uses the Snowflake Ingest SDK to write rows directly into tables without file staging. Both are serverless. Snowpipe Streaming achieves sub-second latency vs seconds-to-minutes for Snowpipe.'}, {'id': 130, 'domain': '3', 'type': 'single', 'question': 'What is a Git integration in Snowflake?', 'options': ['A connector for loading data from GitHub repositories directly into Snowflake tables', 'A backup system that uses Git version control for snapshotting and versioning table data', 'A way to store Snowflake table data as files in Git repositories for version tracking', 'An integration that connects Snowflake to a Git repository, allowing version-controlled code to be synced and executed'], 'answer': [3], 'explanation': 'Git integration connects Snowflake to remote Git repositories (GitHub, GitLab, etc.). You can create a Git Repository stage to sync code, stored procedures, and UDFs from version control into Snowflake for execution.'}, {'id': 131, 'domain': '5', 'type': 'single', 'question': 'What is a Data Clean Room in Snowflake?', 'options': ['A feature for cleaning, deduplicating, and normalizing data across multiple source tables', 'A secure environment where multiple parties can collaborate on sensitive data without exposing raw data to each other', 'A tool for removing PII from datasets using automated masking and redaction policies', 'A staging area for data validation and quality checks before loading into production tables'], 'answer': [1], 'explanation': "Data Clean Rooms enable privacy-safe data collaboration. Multiple parties can run approved analyses on combined datasets without either party seeing the other's raw data. This is critical for advertising, healthcare, and financial services use cases."}, {'id': 132, 'domain': '5', 'type': 'single', 'question': 'What is a Snowflake Native App?', 'options': ['A desktop client application for running SQL queries against Snowflake with syntax highlighting', 'A pre-built dashboard template that can be customized with Snowflake data and shared', 'A packaged application built on Snowflake that can be distributed and installed in consumer accounts via the Marketplace', 'A mobile application for managing Snowflake accounts, monitoring usage, and viewing alerts'], 'answer': [2], 'explanation': "Native Apps are packaged applications that providers build on Snowflake and distribute via the Marketplace or direct shares. They can include stored procedures, UDFs, Streamlit UIs, and data — all running in the consumer's account."}, {'id': 133, 'domain': '2', 'type': 'single', 'question': 'What is the Trust Center in Snowflake?', 'options': ['A team at Snowflake that handles customer support tickets and escalation procedures', 'A security feature within Snowsight that provides security recommendations, compliance posture, and risk assessments for your account', 'A feature for managing customer-managed encryption keys and key rotation policies', "A public website showing Snowflake's real-time uptime status across all cloud regions"], 'answer': [1], 'explanation': 'Trust Center is a Snowsight feature that provides security recommendations, CIS benchmark compliance, threat detection, and risk assessments. It helps administrators identify and remediate security vulnerabilities in their Snowflake account.'}, {'id': 134, 'domain': '2', 'type': 'single', 'question': 'What is Data Lineage in Snowflake?', 'options': ['A feature that tracks the physical storage location of data across cloud regions and zones', 'A backup and restore mechanism for recovering databases to a specific point in time', 'The ability to trace data flow from source to destination, showing how data moves through tables, views, and transformations', 'A tool for comparing data across different Snowflake accounts to identify synchronization gaps'], 'answer': [2], 'explanation': "Data Lineage in Snowflake tracks how data flows between objects — from source tables through views, transformations, and downstream objects. It is accessible through the ACCESS_HISTORY view and Snowsight's lineage graph."}, {'id': 135, 'domain': '1', 'type': 'multi', 'question': 'Which of the following are valid Snowflake table types? (Select FIVE)', 'options': ['Cached', 'Permanent', 'Temporary', 'Apache Iceberg', 'Dynamic', 'Transient'], 'answer': [1, 2, 3, 4, 5], 'explanation': "Snowflake supports Permanent (default, with Fail-safe), Temporary (session-scoped, no Fail-safe), Transient (no Fail-safe, persists across sessions), Apache Iceberg (open format), Dynamic (auto-refreshing), and External tables. 'Cached' is not a table type."}, {'id': 136, 'domain': '1', 'type': 'single', 'question': 'What are the two types of virtual warehouses in Snowflake?', 'options': ['Standard and Snowpark-Optimized', 'Single-cluster and Multi-cluster', 'Dedicated and Shared', 'Compute-Optimized and Storage-Optimized'], 'answer': [0], 'explanation': 'Snowflake offers Standard warehouses (general purpose) and Snowpark-Optimized warehouses (default 16x more memory per node, configurable up to 64x). Snowpark-Optimized warehouses are ideal for ML training, large-scale data processing, and memory-intensive Snowpark workloads.'}, {'id': 137, 'domain': '1', 'type': 'single', 'question': 'What is Snowflake ML?', 'options': ['A separate product from Snowflake for training and deploying production ML models externally', 'A set of built-in ML capabilities including feature engineering, model training, and model registry within Snowflake', 'A tool for data visualization that creates charts, graphs, and interactive reports', 'A managed notebook environment exclusively for running SQL-based analytics queries'], 'answer': [1], 'explanation': "Snowflake ML provides native ML capabilities including: ML Functions (forecasting, anomaly detection, classification), Feature Store, Model Registry, and integration with popular frameworks — all running within Snowflake's secure environment."}, {'id': 138, 'domain': '2', 'type': 'single', 'question': 'What is OAuth authentication in Snowflake?', 'options': ['A standard authentication protocol that allows third-party applications to access Snowflake without storing user credentials directly', 'A Snowflake-proprietary authentication protocol that replaces standard SAML and OIDC flows', 'A type of network policy that restricts OAuth token exchange to specific IP address ranges', 'A method for encrypting data at rest using rotating AES-256 keys managed by the customer'], 'answer': [0], 'explanation': 'Snowflake supports OAuth (both External OAuth and Snowflake OAuth) allowing third-party tools (Tableau, Power BI, etc.) to authenticate without embedding user credentials. Users authenticate through an identity provider.'}, {'id': 139, 'domain': '2', 'type': 'single', 'question': 'What is key-pair authentication in Snowflake?', 'options': ['Using two passwords to log in — a primary and a secondary for additional verification', 'Sharing encryption keys between accounts to enable cross-account data decryption access', 'A method for sharing data securely between accounts using encrypted direct share links', 'Authentication using an RSA public-private key pair instead of username/password'], 'answer': [3], 'explanation': "Key-pair authentication uses RSA key pairs. The private key stays with the client, and the public key is registered with the Snowflake user. It's commonly used for service accounts, automated processes, and Snowpipe."}, {'id': 140, 'domain': '2', 'type': 'multi', 'question': 'Which of the following are system-defined roles in Snowflake? (Select FIVE)', 'options': ['ORGADMIN', 'ACCOUNTADMIN', 'SECURITYADMIN', 'DATAADMIN', 'USERADMIN', 'SYSADMIN'], 'answer': [0, 1, 2, 4, 5], 'explanation': 'The system-defined roles are ACCOUNTADMIN, SYSADMIN, SECURITYADMIN, USERADMIN, PUBLIC, and ORGADMIN. DATAADMIN is not a system-defined role. ORGADMIN manages organization-level operations across accounts.'}, {'id': 141, 'domain': '3', 'type': 'single', 'question': 'What must you create before defining an external stage that references an S3 bucket?', 'options': ['A network policy that allows outbound connections from Snowflake to the S3 bucket endpoint', 'An external function that connects to the S3 API', 'A resource monitor that tracks the data transfer costs between Snowflake and the S3 bucket', 'A storage integration with the IAM role configuration for S3 access'], 'answer': [3], 'explanation': "Before creating an external stage, you need a storage integration that defines the IAM role and allowed S3 locations. The storage integration avoids embedding credentials in SQL and provides a secure, reusable configuration. Syntax: CREATE STORAGE INTEGRATION ... TYPE = EXTERNAL_STAGE STORAGE_PROVIDER = 'S3' STORAGE_AWS_ROLE_ARN = '...'."}, {'id': 142, 'domain': '3', 'type': 'single', 'question': 'What is the purpose of a Directory Table in Snowflake?', 'options': ['A log of all data loading operations including COPY INTO statistics and error counts', 'A table that catalogs files in a stage, enabling querying of file-level metadata', 'A system table listing all database objects with their creation dates and ownership roles', 'A table that organizes data into directory structures within internal and external stages'], 'answer': [1], 'explanation': 'Directory Tables are built on top of stages and maintain a catalog of staged files with metadata (file URL, size, last modified, etc.). They enable SQL queries over file metadata and are useful with unstructured data workflows.'}, {'id': 143, 'domain': '4', 'type': 'single', 'question': 'What is the Query Acceleration Service (QAS)?', 'options': ['A feature that caches all query results for faster re-execution across all warehouse sizes', 'A service that offloads portions of a query to serverless compute resources to speed up large scans and filters', 'A tool for automatically rewriting queries to be more efficient using the query optimizer', 'A priority queue system for routing important queries to dedicated compute resources'], 'answer': [1], 'explanation': "QAS offloads parts of eligible queries (large scans, filters) to serverless compute resources, reducing execution time without needing to resize the warehouse. It's best for queries that scan large amounts of data with selective filters."}, {'id': 144, 'domain': '4', 'type': 'single', 'question': "What does 'bytes spilled to local/remote storage' indicate in a Query Profile?", 'options': ['Data was loaded from an external stage and required decompression during the COPY operation', 'The result set was too large to display in Snowsight and was truncated at the row limit', 'Data was successfully cached for future queries in the local warehouse SSD storage layer', 'The warehouse ran out of memory and had to write intermediate results to disk or remote storage'], 'answer': [3], 'explanation': 'Spilling indicates the warehouse ran out of memory for processing. Spilling to local storage (SSD) is less severe; spilling to remote storage is worse. Solutions: use a larger warehouse or optimize the query to reduce data volume.'}, {'id': 145, 'domain': '5', 'type': 'single', 'question': 'What is the difference between a Direct Share and a Listing in Snowflake?', 'options': ['Direct shares are account-to-account sharing; Listings are published to the Marketplace for broader discovery', 'Direct shares are faster than listings because they bypass the Marketplace discovery layer', 'Listings are always free to consumers; Direct shares always require a paid subscription', 'Both Direct Shares and Listings use the same underlying mechanism with identical visibility'], 'answer': [0], 'explanation': 'Direct Shares provide point-to-point sharing between specific accounts. Listings are published to the Snowflake Marketplace (public or private) where any consumer can discover and request access. Listings support monetization and broader distribution.'}, {'id': 146, 'domain': '5', 'type': 'multi', 'question': 'Which of the following can be shared via Secure Data Sharing? (Select THREE)', 'options': ['Secure UDFs', 'Tables', 'Secure Views', 'Warehouses', 'Roles'], 'answer': [0, 1, 2], 'explanation': 'Secure Data Sharing can include tables, secure views, and secure UDFs in a share. Warehouses and roles cannot be shared — the consumer uses their own warehouse and the share is accessed via a database created from the share.'}, {'id': 147, 'domain': '1', 'type': 'single', 'question': 'What is Cortex Analyst?', 'options': ['A generative AI assistant that writes and debugs SQL queries by analyzing table schemas and sample data', 'A Snowflake Cortex service that enables natural language text-to-SQL querying over structured data using semantic models', 'A cost optimization advisor that identifies unused warehouses and suggests auto-suspend settings', 'A user activity monitoring service that tracks login patterns and data access across roles'], 'answer': [1], 'explanation': 'Cortex Analyst enables business users to ask questions in natural language and get SQL-generated answers from their structured data. It uses semantic models (YAML definitions) that map business terms to database objects.'}, {'id': 148, 'domain': '1', 'type': 'single', 'question': 'What is Cortex Search?', 'options': ['A feature for searching query history by keywords, users, warehouses, and time ranges', 'A SQL LIKE clause replacement that uses vector embeddings for approximate string matching', 'A fully managed hybrid search service that combines keyword and semantic (vector) search over text data in Snowflake', 'A tool for searching the Snowflake Marketplace by data category, provider, and pricing'], 'answer': [2], 'explanation': 'Cortex Search provides hybrid search (keyword + semantic/vector) over text data stored in Snowflake. It automatically creates and manages embeddings and indexes, enabling RAG (Retrieval-Augmented Generation) applications.'}, {'id': 149, 'domain': '2', 'type': 'single', 'question': 'What is a Privacy Policy in Snowflake?', 'options': ['A setting that encrypts all data in transit between Snowflake clients and the service endpoint', 'A policy that controls what data can be returned from queries, using privacy-enhancing techniques like aggregation constraints and differential privacy', 'A legal document displayed to users at login that must be accepted before account access', 'A network firewall rule that blocks traffic from unauthorized IP addresses and regions'], 'answer': [1], 'explanation': 'Privacy Policies in Snowflake apply privacy-enhancing techniques to query results. They can enforce minimum aggregation group sizes and add noise to results (differential privacy), preventing re-identification of individuals in datasets.'}, {'id': 150, 'domain': '3', 'type': 'single', 'question': 'What is an API Integration in Snowflake?', 'options': ['A Snowflake object that allows external functions and Snowpark to call external APIs securely', 'A monitoring endpoint for checking Snowflake service health, latency, and availability', 'A REST API for managing Snowflake accounts including creating users, roles, and warehouses', 'A tool for importing data from external REST APIs into Snowflake tables on a schedule'], 'answer': [0], 'explanation': 'An API Integration is a Snowflake object that configures the security and endpoint information needed for external functions to call external HTTP APIs. It enables Snowflake to securely interact with cloud services like AWS API Gateway or Azure API Management.'}, {'id': 151, 'domain': '4', 'type': 'single', 'question': 'Which types of queries benefit most from the Search Optimization Service?', 'options': ['Queries that scan all rows in a table for full aggregation operations like COUNT or SUM', 'Queries that join multiple large tables together using hash join or merge join strategies', 'Selective point lookup queries (equality), substring searches, and queries on VARIANT fields', 'INSERT and UPDATE DML operations on large tables with millions of rows per transaction'], 'answer': [2], 'explanation': "SOS is optimized for selective queries: equality predicates (WHERE id = 123), substring/regex searches (LIKE '%pattern%'), and queries on semi-structured VARIANT fields. It is NOT beneficial for full table scans, aggregations, or DML operations. It works alongside (not instead of) clustering keys."}, {'id': 152, 'domain': '4', 'type': 'single', 'question': 'What does a high clustering depth value returned by SYSTEM$CLUSTERING_INFORMATION indicate?', 'options': ['The data is poorly clustered on the specified columns, meaning queries will scan more micro-partitions than necessary', 'The table has too many micro-partitions and needs manual compaction to reduce overhead', 'The clustering key has too many columns defined and should be reduced for efficiency', 'The table is well-clustered and queries will prune micro-partitions very efficiently'], 'answer': [0], 'explanation': 'A high average clustering depth means data is NOT well-organized by those columns — micro-partitions have wide, overlapping value ranges. Lower depth = better clustering = better pruning. Use Automatic Clustering (Enterprise+) to maintain low depth over time.'}, {'id': 153, 'domain': '4', 'type': 'multi', 'question': 'What causes the query result cache to be invalidated in Snowflake?', 'options': ['Suspending and resuming the virtual warehouse', 'The query result cache never expires until manually cleared', 'Any DML operation (INSERT, UPDATE, DELETE, MERGE) on the underlying tables', "Changing the user's role or switching warehouses"], 'answer': [2], 'explanation': "The result cache is invalidated when underlying data changes via DML operations (INSERT, UPDATE, DELETE, MERGE, COPY INTO). It also expires after 24 hours. Suspending/resuming a warehouse does NOT invalidate the result cache (it's in the Cloud Services Layer, not the warehouse). Role changes don't affect it either."}, {'id': 154, 'domain': '4', 'type': 'single', 'question': 'Which cache layer in Snowflake does NOT require a running virtual warehouse to serve results?', 'options': ['Warehouse (local disk) cache', 'Compute cache', 'SSD cache', 'Query result cache'], 'answer': [3], 'explanation': "The query result cache is stored in the Cloud Services layer, not in the warehouse. If the same query is re-run within 24 hours and data hasn't changed, results are returned instantly with no warehouse needed (no compute cost)."}, {'id': 155, 'domain': '4', 'type': 'single', 'question': "What does 'inefficient pruning' in a Query Profile indicate?", 'options': ['The result set is too large and exceeds the maximum row count for the client driver', 'The query uses too many JOIN operations and should be rewritten with fewer table references', 'The query is scanning more micro-partitions than necessary because data is not well-clustered on the filter columns', 'Too many warehouses are running simultaneously and competing for shared infrastructure'], 'answer': [2], 'explanation': "Inefficient pruning means Snowflake cannot skip micro-partitions effectively. This typically happens when filter/WHERE columns don't align with how data is physically organized. Solutions: add a clustering key on filter columns, or restructure the query."}, {'id': 156, 'domain': '4', 'type': 'single', 'question': "What is an 'exploding join' as shown in a Query Profile?", 'options': ['A JOIN that produces significantly more rows than the input tables due to many-to-many matches', 'A JOIN that causes the warehouse to crash due to insufficient memory allocation per node', 'A JOIN between tables in different databases that requires cross-database privilege grants', 'A JOIN that uses too much memory because the build side exceeds the available hash table size'], 'answer': [0], 'explanation': 'An exploding join occurs when a JOIN produces a Cartesian-like result with far more output rows than input rows, usually from many-to-many key matches or missing/incorrect join conditions. The Query Profile shows this as a large row count increase at the Join operator.'}, {'id': 157, 'domain': '4', 'type': 'single', 'question': 'If you notice queries frequently queuing on a warehouse used by multiple BI dashboards, what is the recommended solution?', 'options': ['Increase the warehouse size from Medium to X-Large to provide more compute per query', 'Enable multi-cluster warehouses to automatically scale out additional clusters for the concurrent load', 'Increase the AUTO_SUSPEND timeout to keep the warehouse running longer between query bursts', 'Create a Resource Monitor to limit the number of concurrent queries that can execute at once'], 'answer': [1], 'explanation': "For concurrency issues (many queries queuing), the recommended solution is multi-cluster warehouses (Enterprise+), which scale OUT by adding clusters. Scaling UP (bigger size) helps individual query speed but doesn't help concurrency. Resource Monitors control credits, not concurrency."}, {'id': 158, 'domain': '4', 'type': 'single', 'question': 'What is a window function in SQL?', 'options': ['A function that opens a new database connection to an external system via an API integration', 'A function that performs calculations across a set of rows related to the current row without collapsing them into a single output row', 'A function that creates temporary tables during execution for intermediate result storage', 'A function that filters rows based on time windows defined by INTERVAL or DATE_TRUNC parameters'], 'answer': [1], 'explanation': "Window functions (e.g., ROW_NUMBER(), RANK(), LAG(), LEAD(), SUM() OVER()) compute values across a 'window' of rows defined by PARTITION BY and ORDER BY clauses. Unlike GROUP BY, they return one result per input row."}, {'id': 159, 'domain': '4', 'type': 'single', 'question': 'How do you query semi-structured data (JSON) stored in a VARIANT column in Snowflake?', 'options': ['You must first convert it to a relational table using an ETL tool or external process', 'Use a special JSON query language separate from SQL that is specific to Snowflake', "Use dot notation or bracket notation to traverse the JSON hierarchy (e.g., col:key or col['key'])", 'Semi-structured data must be extracted into relational columns before it can be filtered'], 'answer': [2], 'explanation': "Snowflake natively supports querying VARIANT/OBJECT/ARRAY columns using dot notation (col:key.subkey) or bracket notation (col['key']). You can also use FLATTEN() to explode arrays, and LATERAL FLATTEN for nested structures."}, {'id': 160, 'domain': '4', 'type': 'single', 'question': 'What is a best practice for workload management in Snowflake?', 'options': ['Group similar workloads onto dedicated warehouses (e.g., separate ETL, BI, and ad-hoc warehouses)', 'Run all queries on a single large warehouse for simplicity and easier credit tracking', 'Always use the smallest warehouse size available to minimize per-second credit consumption', 'Disable auto-suspend to keep warehouses always ready and eliminate cold-start query latency'], 'answer': [0], 'explanation': 'Snowflake recommends grouping similar workloads onto dedicated warehouses. This isolates resource consumption, prevents contention between workload types, and allows independent sizing/scaling. ETL, BI reporting, and ad-hoc queries should have separate warehouses.'}, {'id': 161, 'domain': '4', 'type': 'single', 'question': 'What is the correct syntax to expand a JSON array stored in a VARIANT column into individual rows?', 'options': ['SELECT value FROM table, UNNEST(col:items) which auto-expands array elements into rows', 'SELECT ARRAY_TO_ROWS(col:items) FROM table to convert each array element to a row', 'SELECT f.value FROM table, LATERAL FLATTEN(input => col:items) f', 'SELECT EXPAND(col:items) FROM table which recursively flattens nested structures'], 'answer': [2], 'explanation': 'LATERAL FLATTEN with the input parameter is the correct syntax. LATERAL allows FLATTEN to reference columns from the preceding table expression. The alias (f) gives access to the flattened output columns: value, index, key, path, this. UNNEST, EXPAND, and ARRAY_TO_ROWS are not Snowflake functions.'}, {'id': 162, 'domain': '4', 'type': 'single', 'question': 'What does the Query Profile in Snowsight help you identify?', 'options': ['Only the estimated cost of a query in credits based on warehouse size and execution time', 'Only the number of rows returned by the query along with basic timing information', 'Only the user who ran the query and the warehouse that was used for execution', 'Performance bottlenecks including data spilling, inefficient pruning, exploding joins, and operator-level execution details'], 'answer': [3], 'explanation': 'The Query Profile provides a visual, operator-level breakdown of query execution. It shows statistics like bytes scanned, spilling, pruning efficiency, join explosions, and time spent per operator — critical for diagnosing and optimizing slow queries.'}, {'id': 163, 'domain': '1', 'type': 'single', 'question': 'How does Snowflake handle unstructured data such as images, PDFs, and videos?', 'options': ['Unstructured data must be converted to CSV format before it can be loaded into Snowflake', 'Snowflake stores unstructured data as base64-encoded strings inside VARIANT columns', 'Unstructured data is stored directly in VARIANT columns alongside semi-structured data', 'Unstructured files are stored in stages and accessed via scoped URLs, file URLs, or pre-signed URLs'], 'answer': [3], 'explanation': 'Snowflake supports unstructured data by storing files in internal or external stages. Access is via three URL types: scoped URLs (temporary, user-specific), file URLs (permanent, privilege-based), or pre-signed URLs (open access, time-limited). Directory tables catalog these files.'}, {'id': 164, 'domain': '1', 'type': 'single', 'question': 'What function generates a temporary URL to access an unstructured file stored in a Snowflake stage?', 'options': ['BUILD_SCOPED_FILE_URL', 'GET_PRESIGNED_URL', 'GET_STAGE_LOCATION', 'CREATE_TEMPORARY_URL'], 'answer': [1], 'explanation': 'GET_PRESIGNED_URL generates a pre-signed HTTPS URL with a time-limited access token for accessing staged files without Snowflake authentication. BUILD_SCOPED_FILE_URL creates user-specific scoped URLs. BUILD_STAGE_FILE_URL creates permanent file URLs.'}, {'id': 165, 'domain': '1', 'type': 'multi', 'question': 'Which of the following are valid Snowflake data type categories? (Select THREE)', 'options': ['Semi-structured (VARIANT, OBJECT, ARRAY)', 'Numeric (NUMBER, FLOAT)', 'Blockchain (LEDGER)', 'String (VARCHAR, STRING)', 'Graph (GRAPH, NODE)'], 'answer': [0, 1, 3], 'explanation': 'Snowflake supports Numeric (NUMBER, INT, FLOAT, etc.), String (VARCHAR, CHAR, STRING, TEXT), Semi-structured (VARIANT, OBJECT, ARRAY), Boolean, Date/Time (DATE, TIMESTAMP, TIME), Binary, and Geospatial types. There are no Graph or Blockchain types.'}, {'id': 166, 'domain': '1', 'type': 'single', 'question': 'What is a Snowflake External Table?', 'options': ['A table stored in another Snowflake account that is accessed through a secure data share', 'A temporary table created specifically for ETL pipeline staging and transformation steps', 'A table with external access to third-party APIs through an external function integration', 'A read-only table that references data in external cloud storage without loading it into Snowflake'], 'answer': [3], 'explanation': 'External tables are read-only and reference data files in external cloud storage (S3, Azure Blob, GCS). They allow querying data in place without loading it. They can have auto-refresh enabled via cloud event notifications.'}, {'id': 167, 'domain': '1', 'type': 'single', 'question': 'What is the maximum compressed size of a single VARIANT value in Snowflake?', 'options': ['16 MB', '8 MB', '4 MB', '64 MB'], 'answer': [0], 'explanation': 'A single VARIANT value can be up to 16 MB compressed. This is important when loading large JSON documents — documents exceeding 16 MB must be split before loading.'}, {'id': 168, 'domain': '2', 'type': 'single', 'question': 'What is the minimum billing period when a virtual warehouse is started?', 'options': ['30 seconds minimum', '5 minutes minimum', '10 minutes minimum', '60 seconds minimum'], 'answer': [3], 'explanation': 'Virtual warehouses have a minimum billing of 60 seconds (1 minute) when first started or resumed. After the first minute, billing is per-second. This means even a 5-second query incurs at least 60 seconds of credit charges.'}, {'id': 169, 'domain': '1', 'type': 'single', 'question': 'What is a Secure View in Snowflake?', 'options': ['A view that only ACCOUNTADMIN can create because it requires elevated security privileges', 'A view whose definition and details are hidden from consumers, protecting both data and business logic', 'A view that automatically encrypts all returned data using column-level encryption keys', 'A view that requires multi-factor authentication before any query can access its data'], 'answer': [1], 'explanation': 'Secure views hide the view definition (SQL) from unauthorized users and bypass certain query optimizations to prevent data exposure through side-channel attacks. They are required for sharing data via Secure Data Sharing.'}, {'id': 170, 'domain': '1', 'type': 'single', 'question': 'What is a Sequence in Snowflake?', 'options': ['An ordered list of SQL statements that execute sequentially within a single transaction', 'A method for ordering query results using deterministic sort keys across partitions', 'A type of stored procedure that generates unique identifiers for tracking purposes', 'A schema-level object that generates unique numeric values, often used for surrogate keys'], 'answer': [3], 'explanation': 'A Sequence generates unique, incrementing numeric values. Unlike auto-increment columns, sequences are independent objects that can be shared across tables. Sequences do not guarantee gap-free values.'}, {'id': 171, 'domain': '1', 'type': 'single', 'question': "What is a Materialized View's key limitation compared to a regular view?", 'options': ['Materialized views consume additional storage and compute for maintenance, and have restrictions on supported SQL (e.g., no joins in some cases)', 'Materialized views can only reference a single table and cannot use any aggregate functions', 'Materialized views can only be queried by ACCOUNTADMIN and roles it directly inherits', 'Materialized views support multi-table joins and subqueries just like regular views'], 'answer': [0], 'explanation': 'Materialized views store data physically (extra storage cost) and Snowflake automatically refreshes them (extra compute cost). They have SQL restrictions: limited JOIN support, no UDFs, no window functions, and must query a single table. Requires Enterprise edition.'}, {'id': 172, 'domain': '1', 'type': 'single', 'question': "What is Snowflake's approach to indexing?", 'options': ['Snowflake does not use traditional indexes — it uses micro-partition metadata pruning and optional clustering keys instead', 'Snowflake uses B-tree indexes similar to traditional relational databases for fast lookups', 'Users must manually create indexes on all columns used in WHERE and JOIN clauses', 'Snowflake uses hash indexes exclusively for all table types including transient and temporary'], 'answer': [0], 'explanation': 'Snowflake does NOT use traditional indexes. Instead, it relies on micro-partition metadata (min/max values, distinct counts) for automatic pruning. Clustering keys can further organize data for better pruning. Search Optimization Service adds additional access paths.'}, {'id': 173, 'domain': '1', 'type': 'multi', 'question': 'Which of the following Snowflake features are serverless (no user-managed warehouse required)? (Select THREE)', 'options': ['CREATE TABLE AS SELECT', 'Snowpipe', 'Automatic Clustering', 'Search Optimization Service', 'COPY INTO'], 'answer': [1, 2, 3], 'explanation': 'Snowpipe, Automatic Clustering, and Search Optimization Service are serverless — Snowflake manages the compute. COPY INTO and CREATE TABLE AS SELECT require a user-managed virtual warehouse. Other serverless features include: Tasks (serverless mode), Query Acceleration Service.'}, {'id': 174, 'domain': '1', 'type': 'single', 'question': 'What is the difference between the OBJECT and ARRAY semi-structured data types in Snowflake?', 'options': ['Both OBJECT and ARRAY use the same internal representation with identical query syntax', 'OBJECT stores key-value pairs (like JSON objects); ARRAY stores ordered lists of values (like JSON arrays)', 'ARRAY elements are restricted to numeric integer values and simple scalar types only', 'OBJECT is used exclusively for XML data; ARRAY is used exclusively for JSON arrays'], 'answer': [1], 'explanation': 'OBJECT stores key-value pairs (similar to JSON objects/dictionaries). ARRAY stores ordered, indexed lists of values. Both can be nested inside VARIANT columns. VARIANT is the most general type that can hold any semi-structured value.'}, {'id': 175, 'domain': '1', 'type': 'single', 'question': 'What is the AUTO_RESUME parameter for a virtual warehouse?', 'options': ['Automatically schedules warehouse maintenance windows for cache cleanup and optimization', 'Automatically resizes the warehouse based on current workload intensity and queue depth', 'Automatically recovers from warehouse failures by restarting compute nodes transparently', 'Automatically starts (resumes) a suspended warehouse when a query is submitted to it'], 'answer': [3], 'explanation': 'When AUTO_RESUME is TRUE (default), a suspended warehouse automatically starts when a query is submitted. This provides seamless access without manual intervention. Combined with AUTO_SUSPEND, it enables automatic cost optimization.'}, {'id': 176, 'domain': '2', 'type': 'single', 'question': 'What type of encryption does Snowflake use for data at rest?', 'options': ['AES-128', 'RSA-2048', 'AES-256', 'Blowfish-256'], 'answer': [2], 'explanation': 'Snowflake encrypts all data at rest using AES-256 encryption by default — this is always on and requires no configuration. Data in transit is encrypted with TLS 1.2+. Business Critical edition adds Tri-Secret Secure (customer-managed keys).'}, {'id': 177, 'domain': '1', 'type': 'single', 'question': 'How does Snowflake handle concurrency for a single-cluster warehouse?', 'options': ['Each query gets a dedicated compute node that is isolated from all other concurrent queries', 'Multiple queries run concurrently sharing the warehouse resources; additional queries are queued when resources are exhausted', 'Only one query can run at a time on a warehouse regardless of its size or cluster count', 'Queries are automatically routed to other idle warehouses in the account when capacity is full'], 'answer': [1], 'explanation': 'A single-cluster warehouse can run multiple concurrent queries, sharing compute resources. When all resources are in use, additional queries queue in FIFO order. Multi-cluster warehouses (Enterprise+) add clusters automatically to reduce queuing.'}, {'id': 178, 'domain': '1', 'type': 'single', 'question': 'What is a Snowflake Stage?', 'options': ['A development environment for testing queries before running them against production tables', 'A step in an ETL pipeline where data transformations are applied before final loading', 'A location (internal or external) where data files are stored for loading into or unloading from Snowflake tables', 'A phase in the query execution plan where data is temporarily buffered between operators'], 'answer': [2], 'explanation': 'A stage is a storage location for data files. Internal stages store files in Snowflake-managed storage. External stages reference files in customer-managed cloud storage (S3, Azure Blob, GCS). Stages are used with COPY INTO, PUT, and GET commands.'}, {'id': 179, 'domain': '4', 'type': 'single', 'question': 'What is the purpose of the LATERAL keyword when used with FLATTEN?', 'options': ['It enables parallel execution of the FLATTEN operation across multiple compute nodes', 'It is optional syntax with no functional effect and can be omitted without changing results', 'It forces strict left-to-right evaluation order for all expressions in the SELECT clause', 'It allows FLATTEN to reference columns from the preceding table expression, enabling row-by-row expansion of nested data'], 'answer': [3], 'explanation': 'LATERAL allows a table function (like FLATTEN) to reference columns from a preceding table in the FROM clause. LATERAL FLATTEN is the standard pattern for expanding nested semi-structured data (arrays/objects) into relational rows.'}, {'id': 180, 'domain': '1', 'type': 'single', 'question': "What is Snowflake's approach to software updates and patches?", 'options': ['Updates are applied quarterly during scheduled maintenance outages with advance notification', 'Snowflake transparently handles all updates with zero downtime — no user action required', 'Customers choose when to apply updates from a release menu in the Snowsight admin panel', 'Customers must schedule maintenance windows for updates through a support ticket process'], 'answer': [1], 'explanation': "Snowflake applies updates and patches transparently in the background with zero downtime. Customers do not need to schedule maintenance windows or take any action. This is part of Snowflake's 'zero-maintenance' SaaS model."}, {'id': 181, 'domain': '1', 'type': 'single', 'question': 'What is an External Function in Snowflake?', 'options': ['A function written in Python or Java that executes entirely within the Snowflake warehouse', 'A built-in function for accessing external tables and querying data in cloud storage directly', 'A function that runs entirely on the client side without consuming any warehouse credits', 'A user-defined function that calls an external HTTP API endpoint (e.g., AWS API Gateway, Azure API Management) via an API Integration'], 'answer': [3], 'explanation': "External functions call remote HTTP endpoints (REST APIs) during query execution. They require an API Integration object for security configuration. Use cases include calling ML models, third-party services, or custom processing that can't run natively in Snowflake."}, {'id': 182, 'domain': '1', 'type': 'single', 'question': 'What is the Snowflake Data Cloud rebranded as in COF-C03?', 'options': ['Snowflake AI Data Cloud', 'Snowflake Data Lake', 'Snowflake Data Mesh', 'Snowflake Data Warehouse'], 'answer': [0], 'explanation': "Snowflake officially uses 'Snowflake AI Data Cloud' to reflect the platform's expanded capabilities beyond data warehousing — including AI/ML, data sharing, applications, and collaboration across the ecosystem."}, {'id': 183, 'domain': '3', 'type': 'multi', 'question': 'Which of the following are Snowflake drivers for programmatic connectivity? (Select THREE)', 'options': ['JDBC', 'ODBC', 'Telnet Driver', 'Python Connector', 'FTP Client'], 'answer': [0, 1, 3], 'explanation': 'Snowflake provides drivers/connectors for: JDBC (Java), ODBC (C/C++), Python, Node.js, Go, .NET, and PHP PDO. These enable programmatic access from applications. FTP and Telnet are not Snowflake connectivity options.'}, {'id': 184, 'domain': '3', 'type': 'single', 'question': 'What is the difference between SnowSQL and the Snowflake CLI (snow)?', 'options': ['SnowSQL is the newer replacement for Snowflake CLI and includes all of its functionality', 'Snowflake CLI focuses on managing warehouses, stages, and compute resource configurations', 'They are the same tool with different names — Snowflake CLI is just the rebranded SnowSQL', 'SnowSQL is for executing SQL queries; Snowflake CLI (snow) is for managing Snowflake objects like Streamlit apps, Snowpark projects, and Native Apps'], 'answer': [3], 'explanation': 'SnowSQL is a command-line client primarily for executing SQL queries and scripts. Snowflake CLI (snow command) is a modern developer tool for managing Snowflake objects: deploying Streamlit apps, Snowpark projects, Native Apps, notebooks, and Git repos.'}, {'id': 185, 'domain': '3', 'type': 'single', 'question': 'Which Snowflake connectivity method would you use to connect a Java application to Snowflake?', 'options': ['Go Driver', 'Kafka Connector', 'JDBC Driver', 'ODBC Driver'], 'answer': [2], 'explanation': 'The JDBC (Java Database Connectivity) Driver is used to connect Java applications to Snowflake. It implements the standard JDBC interface, allowing any JDBC-compatible Java application or tool (like IntelliJ, DBeaver) to connect.'}, {'id': 186, 'domain': '3', 'type': 'single', 'question': 'What connectivity interface would you use to connect a BI tool like Tableau or Power BI to Snowflake?', 'options': ['Snowflake CLI (snow) command-line tool with native BI integration support built in', "ODBC or JDBC driver, or the tool's native Snowflake connector", 'Snowpipe REST API for continuous ingestion that also handles BI tool query connections', 'Python Connector only, which is the exclusive method for programmatic BI tool access'], 'answer': [1], 'explanation': 'BI tools typically connect via ODBC, JDBC, or their own native Snowflake connector. Tableau and Power BI both have built-in Snowflake connectors. ODBC/JDBC are the standard interfaces for tool connectivity.'}, {'id': 187, 'domain': '3', 'type': 'single', 'question': 'What protocol does Snowflake use for all client-server communication?', 'options': ['SSH (Secure Shell)', 'HTTPS (TLS 1.2 or higher)', 'JDBC over TCP/IP', 'FTP with SSL encryption'], 'answer': [1], 'explanation': 'All Snowflake client-server communication uses HTTPS with TLS 1.2 or higher encryption. This applies to all drivers (JDBC, ODBC, Python, etc.), the web UI (Snowsight), and REST API calls. There is no option to use unencrypted connections.'}, {'id': 188, 'domain': '2', 'type': 'single', 'question': 'What is a Projection Policy in Snowflake?', 'options': ['A column-level policy that prevents specific columns from being included in query results via SELECT', 'A policy that projects data across multiple databases by creating cross-database virtual views', 'A policy that limits query execution time to prevent long-running queries from consuming credits', 'A type of network security rule that restricts outbound traffic from Snowflake to external APIs'], 'answer': [0], 'explanation': "Projection Policies prevent columns from appearing in query results. Unlike masking policies (which return masked values), projection policies block the column entirely — queries that include the column fail unless the user's role is allowed. Requires Enterprise edition."}, {'id': 189, 'domain': '2', 'type': 'single', 'question': 'What is data classification in Snowflake?', 'options': ['An automated process that analyzes column data and metadata to identify sensitive information (PII, financial data, etc.) and assign system tags', 'Organizing data into folders and directories within stages for structured file management', 'A manual process of labeling each row with sensitivity tags using UPDATE statements', 'A method for compressing data using columnar encoding to reduce storage costs in Snowflake'], 'answer': [0], 'explanation': "Snowflake's data classification uses the SYSTEM$CLASSIFY function to automatically scan columns and identify sensitive data categories (name, email, phone, SSN, etc.). It assigns system tags for governance. Can be run manually or set up for automatic classification."}, {'id': 190, 'domain': '2', 'type': 'single', 'question': 'What is the IMPORTED PRIVILEGES grant on the SNOWFLAKE database used for?', 'options': ['Granting a role access to ACCOUNT_USAGE and other shared views in the SNOWFLAKE system database', 'Importing data from external sources like S3, Azure Blob, or GCS into Snowflake tables', 'Enabling cross-database queries by granting SELECT on tables in one database to another role', 'Importing privileges from another Snowflake account through cross-account role replication'], 'answer': [0], 'explanation': 'The SNOWFLAKE database is a system-shared database. To query its views (ACCOUNT_USAGE, ORGANIZATION_USAGE, etc.), a role needs the IMPORTED PRIVILEGES grant: GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE my_role.'}, {'id': 191, 'domain': '4', 'type': 'single', 'question': 'How do you enable the Query Acceleration Service for a warehouse?', 'options': ['Create a new warehouse with the ACCELERATED type parameter set to TRUE in the DDL statement', 'Enable it in the Snowsight warehouse settings under the Advanced configuration tab', 'ALTER WAREHOUSE SET ENABLE_QUERY_ACCELERATION = TRUE with an optional QUERY_ACCELERATION_MAX_SCALE_FACTOR', 'Contact Snowflake support to enable query acceleration as it requires a backend feature flag'], 'answer': [2], 'explanation': 'QAS is enabled per-warehouse using ALTER WAREHOUSE my_wh SET ENABLE_QUERY_ACCELERATION = TRUE. You can also set QUERY_ACCELERATION_MAX_SCALE_FACTOR to control the maximum serverless compute allocated (default is 8). Use SYSTEM$ESTIMATE_QUERY_ACCELERATION to check if queries would benefit.'}, {'id': 192, 'domain': '4', 'type': 'single', 'question': 'What is the recommended approach for optimizing a query that scans too many micro-partitions?', 'options': ['Increase the AUTO_SUSPEND timeout so the warehouse stays warm and avoids cold start latency', 'Convert the table from permanent to transient to reduce the overhead of micro-partition metadata tracking', 'Drop and recreate the table to reset micro-partition statistics and storage layout', 'Define or adjust a clustering key on the columns used in WHERE/JOIN clauses to improve pruning'], 'answer': [3], 'explanation': 'When queries scan excessive micro-partitions (poor pruning), adding a clustering key on the filter/join columns reorganizes data so Snowflake can skip irrelevant partitions. Use SYSTEM$CLUSTERING_INFORMATION to assess current clustering depth before and after.'}, {'id': 193, 'domain': '4', 'type': 'single', 'question': 'How long do query results remain in the result cache?', 'options': ['24 hours, as long as the underlying data has not changed', '48 hours, regardless of whether the underlying data has been modified since the query ran', '7 days after the query was first executed, regardless of data changes or warehouse state', '1 hour after execution, after which results must be recomputed from the base tables'], 'answer': [0], 'explanation': "Query result cache persists for 24 hours in the Cloud Services layer. If the same query is resubmitted within 24 hours and the underlying data hasn't changed, results are returned instantly at zero compute cost. Any DML changes to underlying tables invalidate the cache."}, {'id': 194, 'domain': '4', 'type': 'single', 'question': 'What tool can you use to see the clustering quality of a table?', 'options': ['SHOW TABLES command which displays table metadata including row counts and storage sizes', 'QUERY_HISTORY view which tracks execution details, duration, and warehouse utilization', 'DESCRIBE TABLE command which shows column names, data types, and default values', 'SYSTEM$CLUSTERING_INFORMATION function'], 'answer': [3], 'explanation': 'SYSTEM$CLUSTERING_INFORMATION returns clustering depth, overlap, and other metrics for a table on specified columns. Lower average clustering depth means better clustering (more effective pruning). This helps decide whether to add or change clustering keys.'}, {'id': 195, 'domain': '4', 'type': 'single', 'question': 'What is the difference between a deterministic and non-deterministic query for result caching purposes?', 'options': ['All query types benefit equally from the result cache regardless of their SQL structure', 'Only non-deterministic queries use the cache because deterministic queries are always fast', 'Both deterministic and non-deterministic queries always use the cache without restrictions', 'Deterministic queries (same SQL, same data = same result) can use result cache; non-deterministic queries (using CURRENT_TIMESTAMP, RANDOM, etc.) cannot'], 'answer': [3], 'explanation': 'The result cache only works for deterministic queries — those that produce the same result given the same data. Queries using functions like CURRENT_TIMESTAMP(), RANDOM(), UUID_STRING(), or CURRENT_DATE() are non-deterministic and bypass the result cache.'}, {'id': 196, 'domain': '4', 'type': 'single', 'question': 'What is the difference between RANK() and DENSE_RANK() window functions?', 'options': ['RANK() leaves gaps in ranking after ties; DENSE_RANK() assigns consecutive ranks with no gaps', 'DENSE_RANK() is faster than RANK() because it skips the internal sorting step entirely', 'RANK() and DENSE_RANK() both produce consecutive ranks and handle ties identically', 'RANK() allows ties in ranking while DENSE_RANK() breaks ties using the row insertion order'], 'answer': [0], 'explanation': 'Both handle ties the same way (tied rows get the same rank), but RANK() leaves gaps after ties (1,1,3) while DENSE_RANK() does not (1,1,2). ROW_NUMBER() never has ties — it assigns unique sequential numbers. All three are commonly used with OVER(ORDER BY ...).'}, {'id': 197, 'domain': '4', 'type': 'multi', 'question': 'Which of the following are common window functions in Snowflake? (Select THREE)', 'options': ['COPY_INTO()', 'LAG()', 'ROW_NUMBER()', 'FLATTEN()', 'RANK()'], 'answer': [1, 2, 4], 'explanation': 'ROW_NUMBER(), RANK(), and LAG() are window functions used with the OVER clause. Other common ones: LEAD(), DENSE_RANK(), NTILE(), FIRST_VALUE(), LAST_VALUE(), SUM()/AVG()/COUNT() OVER(). FLATTEN is a table function; COPY INTO is a command.'}, {'id': 198, 'domain': '1', 'type': 'single', 'question': 'What is the minimum AUTO_SUSPEND value and what does setting it to 0 mean?', 'options': ['Minimum is 1 second; 0 disables auto-suspend (warehouse runs indefinitely)', 'The minimum value is 60 seconds, and setting it to 0 keeps the warehouse always running', 'The minimum AUTO_SUSPEND is 30 seconds, and setting it to 0 disables the warehouse permanently', 'AUTO_SUSPEND only accepts values in minutes (not seconds) with a minimum of 1 minute'], 'answer': [0], 'explanation': 'AUTO_SUSPEND can be set as low as 1 second (though very low values may cause frequent resume overhead). Setting it to 0 or NULL disables auto-suspend entirely, meaning the warehouse runs until manually suspended. Best practice: 60-300 seconds for most workloads to balance cost and responsiveness.'}, {'id': 199, 'domain': '2', 'type': 'single', 'question': 'What is the purpose of the SHOW GRANTS command in Snowflake?', 'options': ['Listing all users in the account with their roles, login history, and MFA enrollment status', 'Displaying privileges granted on objects, to roles, or by roles', 'Showing warehouse credit grants and consumption quotas assigned to each warehouse', 'Displaying all databases in the account with their creation dates and retention settings'], 'answer': [1], 'explanation': 'SHOW GRANTS displays privilege information. SHOW GRANTS ON <object> shows who has access. SHOW GRANTS TO ROLE <role> shows what a role can access. SHOW GRANTS OF ROLE <role> shows which users/roles have been granted the role.'}, {'id': 200, 'domain': '2', 'type': 'single', 'question': "What is the principle of least privilege in Snowflake's access control?", 'options': ['Give all users ACCOUNTADMIN access for simplicity to avoid managing complex role hierarchies', 'Grant users and roles only the minimum privileges needed to perform their tasks', 'Use a single role for all operations to simplify access control and reduce administrative work', 'Disable access control entirely to reduce complexity and improve query execution performance'], 'answer': [1], 'explanation': 'Least privilege is a security best practice: grant only the permissions needed. In Snowflake, this means using custom roles with specific grants rather than over-using ACCOUNTADMIN, and creating separate roles for different functional needs.'}]

# ---------------------------------------------------------------------------
# LLM Question Generation
# ---------------------------------------------------------------------------
GEN_MODEL    = "claude-opus-4-8"     # generation: higher quality, fewer retries
VERIFY_MODEL = "claude-sonnet-4-6"   # verification: fast fact-check, already capable
MAX_GENERATION_ATTEMPTS = 3
VARIATIONS_PER_CONCEPT = 2  # questions generated per concept; raise to increase domain depth
BULK_BATCH_SIZE     = 5         # concepts generated per Cortex call during pre-load
PARALLEL_GEN_BATCHES = 4        # number of generation batches to fire in parallel per rerun


def _call_cortex(prompt: str):
    """Execute a CORTEX.COMPLETE call. Returns raw string or None on failure."""
    conn = get_snowflake_connection()
    if conn is None:
        return None
    try:
        cur = conn.cursor()
        # Embed a uniqueness token directly in the PROMPT (not a SQL comment —
        # Snowflake normalizes comments out of the result-cache key, and a
        # session-level USE_CACHED_RESULT toggle is skipped when the cached
        # connection is reused across reruns). Changing the prompt text is the
        # one thing guaranteed to force a cache miss so the model re-runs. The
        # model ignores this trailing line.
        prompt = f"{prompt}\n\n[request id, ignore: {time.time_ns():x}{random.getrandbits(32):08x}]"
        escaped = prompt.replace("\\", "\\\\").replace("'", "''")
        cur.execute(
            f"SELECT SNOWFLAKE.CORTEX.COMPLETE('{GEN_MODEL}', '{escaped}')"
        )
        return cur.fetchone()[0]
    except Exception as e:
        log_error(f"Cortex call failed: {e}")
        return None


def _adjudicate_challenge(q: dict, user_answer: list, user_argument: str) -> dict:
    """Adjudicate a user's challenge to a question's answer key, grounded in the
    question's cited Snowflake documentation.

    Returns a dict:
      {"status": "ok",  "verdict": "upheld"|"rejected", "reasoning": str,
       "corrected_answer": [int], "corrected_explanation": str}   # last two only when upheld
      {"status": "error", "message": str}                          # nothing changed

    "upheld" means the user is right and the answer key was wrong. Conservative:
    only overturns when the cited docs CLEARLY support the change.
    """
    urls = [c.get("url") for c in (q.get("citations") or []) if c.get("url")]
    if not urls:
        return {"status": "error",
                "message": "This question has no documentation citations, so it can't be auto-adjudicated."}

    # Fetch the cited pages to ground the ruling (the same evidence the verifier uses).
    fetched = []
    for u in urls:
        content = _fetch_doc_content(u)
        if content:
            fetched.append(f"URL: {u}\n---\n{content[:6000]}")
    if not fetched:
        return {"status": "error",
                "message": "The cited documentation pages couldn't be retrieved right now, so the challenge can't be auto-adjudicated. Try again shortly."}

    options_block = "\n".join(f"  [{i}] {o}" for i, o in enumerate(q.get("options", [])))
    docs_block = "\n\n".join(fetched)
    prompt = f"""You are a strict, impartial Snowflake certification fact-checker adjudicating a user's dispute of an exam question's answer key. Rule ONLY on the basis of the Snowflake documentation text provided below — not on general knowledge or intuition.

QUESTION:
{q.get('question', '')}

OPTIONS (index in brackets):
{options_block}

CURRENT ANSWER KEY (indices marked correct): {q.get('answer', [])}
CURRENT EXPLANATION: {q.get('explanation', '')}

THE USER SELECTED (indices): {sorted(user_answer)}
THE USER'S ARGUMENT THAT THE ANSWER KEY IS WRONG:
{user_argument}

DOCUMENTATION (authoritative — the ONLY basis for your ruling):
{docs_block}

Decide whether the user's challenge is substantiated by the documentation above.
- Be CONSERVATIVE: uphold the challenge ONLY if the documentation CLEARLY shows the current answer key is wrong. If the documentation does not clearly support a change, REJECT the challenge and keep the current answer.
- If you uphold it, return the FULL corrected set of correct option indices (for multi-select, list ALL correct indices) and a corrected explanation that cites the specific documentation text.

Return ONLY valid JSON with no markdown fences:
{{"verdict": "upheld" | "rejected",
  "corrected_answer": [<integer indices>],
  "corrected_explanation": "<doc-grounded explanation of the correct answer>",
  "reasoning": "<one paragraph citing the specific documentation text that justifies your ruling>"}}"""

    raw = _call_cortex(prompt)
    if raw is None:
        return {"status": "error",
                "message": "Couldn't reach Cortex to evaluate the challenge. Check your connection and try again."}
    parsed = _parse_json(raw)
    if not isinstance(parsed, dict) or parsed.get("verdict") not in ("upheld", "rejected"):
        return {"status": "error",
                "message": "The adjudicator's response couldn't be parsed. Please try again."}

    verdict = parsed["verdict"]
    result = {"status": "ok", "verdict": verdict, "reasoning": str(parsed.get("reasoning", "")).strip()}
    if verdict == "upheld":
        n_opts = len(q.get("options", []))
        ca = parsed.get("corrected_answer", [])
        if isinstance(ca, int):
            ca = [ca]
        ca = sorted({i for i in ca if isinstance(i, int) and 0 <= i < n_opts})
        if not ca:
            # Upheld but no valid corrected answer — refuse to change anything rather than corrupt the question.
            return {"status": "error",
                    "message": "The adjudicator upheld the challenge but returned no valid corrected answer, so nothing was changed. Please try again."}
        result["corrected_answer"] = ca
        result["corrected_explanation"] = str(parsed.get("corrected_explanation", "")).strip() or q.get("explanation", "")
    return result


def _render_challenge_box(cache_key, d_id, q, user_answer):
    """Let the user dispute a question's answer key. Cortex adjudicates against
    the cited docs; if upheld, the cached question is corrected (persisted) and
    the score amended once. Shown in the study answer-reveal view."""
    results = st.session_state.setdefault("challenge_result", {})
    amended = st.session_state.setdefault("score_amended", set())
    ck = f"{cache_key[0]}:{cache_key[1]}"

    # Show the prior verdict for this question (persists across the post-submit rerun).
    prior = results.get(ck)
    if prior:
        if prior.get("status") == "error":
            st.warning(prior["message"], icon="⚠️")
        elif prior.get("verdict") == "upheld":
            st.success("Challenge upheld — this question has been corrected.", icon="✅")
            if prior.get("reasoning"):
                st.caption(prior["reasoning"])
        else:
            st.info("The original answer stands — the challenge wasn't substantiated by the docs.", icon="ℹ️")
            if prior.get("reasoning"):
                st.caption(prior["reasoning"])

    with st.expander("🚩 Think this is wrong? Challenge it"):
        st.caption(
            "Explain why you believe the marked answer is incorrect. An adjudicator will "
            "check your argument against this question's cited Snowflake documentation. If it's "
            "substantiated, the question is corrected and your score is updated."
        )
        arg = st.text_area("Your argument", key=f"challenge_arg_{ck}",
                           placeholder="e.g. Per the Resource Monitors doc, the credit quota "
                                       "accounts for both warehouse and cloud services credits in "
                                       "the same threshold, so that option should be correct.")
        if st.button("Submit challenge", key=f"challenge_submit_{ck}"):
            if not arg.strip():
                st.warning("Please enter your argument first.", icon="⚠️")
            else:
                with st.spinner("Adjudicating against the cited documentation…"):
                    res = _adjudicate_challenge(q, user_answer, arg.strip())

                if res.get("status") == "ok" and res.get("verdict") == "upheld":
                    entry = st.session_state.generated_questions.get(cache_key)
                    if entry is not None:
                        old_answer = list(entry.get("answer", []))
                        entry.setdefault("_original_answer", old_answer)
                        entry["answer"] = res["corrected_answer"]
                        entry["explanation"] = res["corrected_explanation"]
                        entry["_corrected"] = True
                        entry["_correction_note"] = res.get("reasoning", "")
                        save_questions_cache()

                        # Amend the score at most once per question.
                        if ck not in amended:
                            was_correct = sorted(user_answer) == sorted(old_answer)
                            now_correct = sorted(user_answer) == sorted(res["corrected_answer"])
                            stats = st.session_state.domain_stats_study[d_id]
                            if not was_correct and now_correct:
                                st.session_state.total_correct += 1
                                stats["correct"] += 1
                                save_progress()
                            elif was_correct and not now_correct:
                                st.session_state.total_correct = max(0, st.session_state.total_correct - 1)
                                stats["correct"] = max(0, stats["correct"] - 1)
                                save_progress()
                            amended.add(ck)
                    else:
                        # Question isn't in the live cache (e.g. offline fallback) — record the
                        # verdict but don't attempt to persist a correction.
                        res = {"status": "error",
                               "message": "This question isn't in the editable cache, so it can't be auto-corrected."}

                results[ck] = res
                st.rerun()


def _parse_json(raw: str):
    """Strip markdown fences and parse JSON. Returns parsed value or None."""
    if not raw:
        return None
    try:
        cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.MULTILINE)
        return json.loads(cleaned)
    except Exception:
        pass
    # Fallback: model wrapped the JSON in prose — extract the outermost array/object
    for open_ch, close_ch in (('[', ']'), ('{', '}')):
        start = raw.find(open_ch)
        end = raw.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except Exception:
                continue
    return None


def _call_cortex_threadsafe(prompt: str, model: str = VERIFY_MODEL, bust_cache: bool = False) -> tuple:
    """
    Thread-safe Cortex call: returns (result_str, error_str).
    Never touches Streamlit state — safe to call from worker threads.
    Defaults to VERIFY_MODEL; generation callers must pass GEN_MODEL.
    Set bust_cache=True for generation to embed a uniqueness token in the PROMPT
    so Snowflake's result cache can't return a prior identical completion. (A
    SQL-comment nonce does NOT work — comments are normalized out of the cache
    key; a session-level USE_CACHED_RESULT toggle is skipped when the cached
    connection is reused across reruns. Changing the prompt text is reliable.)
    Verification leaves it False so identical verdicts stay cached (fast/cheap).
    """
    conn = get_snowflake_connection()
    if conn is None:
        return None, "No Snowflake connection"
    try:
        cur = conn.cursor()
        if bust_cache:
            prompt = f"{prompt}\n\n[request id, ignore: {time.time_ns():x}{random.getrandbits(32):08x}]"
        escaped = prompt.replace("\\", "\\\\").replace("'", "''")
        cur.execute(
            f"SELECT SNOWFLAKE.CORTEX.COMPLETE('{model}', '{escaped}')"
        )
        return cur.fetchone()[0], None
    except Exception as e:
        return None, str(e)


# Auth/session/connection failure signals. Kept deliberately narrow so transient
# content errors (bad JSON, verification misses) never abort a run — only a dead
# token / closed session should.
_CRITICAL_ERROR_SIGNALS = (
    "authentication token has expired",
    "session no longer exists",
    "session has expired",
    "session expired",
    "must authenticate again",
    "authentication token",
    "invalid token",
    "jwt token is invalid",
    "connection is closed",
    "connection already closed",
    "390114",   # Authentication token has expired
    "390108",   # session no longer exists
    "390195",   # session expired
    "250001",   # could not connect / connection closed
    "08003",    # connection does not exist
)


def _is_critical_error(msg: str) -> bool:
    """True if an error string looks like an auth/session/connection failure
    that warrants aborting the whole run (vs a per-question content error)."""
    if not msg:
        return False
    low = str(msg).lower()
    return any(sig in low for sig in _CRITICAL_ERROR_SIGNALS)


def _check_connection() -> tuple:
    """Lightweight pre-flight: run a trivial query to confirm the token/session
    is still alive. Returns (ok: bool, error_msg: str). Called before any bulk
    action that needs Cortex, so an expired session is caught BEFORE work starts
    (and before reset actions wipe the cache)."""
    conn = get_snowflake_connection()
    if conn is None:
        return False, (_LAST_CONN_ERROR or "No Snowflake connection available.")
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        return True, ""
    except Exception as e:
        return False, str(e)


def _verify_question_threadsafe(question: dict, prefetched: dict) -> tuple:
    """
    Thread-safe citation verification for a single question.
    Returns (passed: bool, reason: str, errors: list[str])
    where errors contains messages to be logged by the main thread.
    Never touches Streamlit state.
    """
    citations = question.get("citations", [])
    if not citations:
        return False, "No citations provided", []

    correct_options = [question["options"][i] for i in question["answer"]]
    question_text   = question["question"]
    explanation     = question.get("explanation", "")
    failures = []
    errors   = []

    for c in citations:
        url = c.get("url", "")
        content = (prefetched or {}).get(url) if prefetched else None
        if content is None:
            # Fetch inline (fallback — should rarely happen in batch mode)
            content = _fetch_doc_content(url)
        if content is None:
            failures.append(f"FETCH_FAILED:{url}")
            continue

        prompt = f"""You are a Snowflake certification exam fact-checker. Your job is to verify FACTUAL ACCURACY, not just topical relevance.

Below is the text content retrieved from this documentation page:
URL: {url}
---
{content}
---

Exam question: {question_text}
Stated correct answer(s): {correct_options}
Explanation: {explanation}

Carefully check whether the stated correct answer is factually accurate according to this documentation. Pay close attention to:
- Specific numbers, sizes, thresholds, percentages, or time values (e.g. "100-250 MB", "10%", "90 days")
- Specific behavior descriptions (e.g. "continues loading", "skips the file", "creates a pointer")
- Edition or feature requirements

If the documentation states specific facts that DIFFER from what the answer claims — even if the page is generally relevant to the topic — the citation is invalid.

A citation is valid ONLY if:
- The documentation explicitly confirms or directly supports the specific claim in the correct answer, OR
- The documentation provides directly relevant context that is fully consistent with every specific claim in the answer

A citation is invalid if:
- The documentation states facts that contradict the answer, OR
- The documentation is generally relevant but contains no content that actually supports the specific answer claims, OR
- The page has no meaningful relationship to the question topic

Return ONLY valid JSON with no markdown fences:
{{
  "supported": true,
  "reason": "One sentence citing the specific documentation text that confirms or contradicts the answer."
}}"""

        raw, err = _call_cortex_threadsafe(prompt)
        if err:
            errors.append(f"Cortex call failed for {url}: {err}")
            failures.append(f"Cortex verification call failed for {url}")
            continue
        if raw is None:
            failures.append(f"Cortex verification call failed for {url}")
            continue

        result = _parse_json(raw)
        if result is None:
            failures.append(f"Could not parse verifier response for {url}")
            continue

        if not result.get("supported", False):
            reason = result.get("reason", "no reason given")
            failures.append(f"{url} — {reason}")

    if failures:
        all_fetch_failures = all(f.startswith("FETCH_FAILED:") for f in failures)
        if all_fetch_failures:
            failed_urls = [f[len("FETCH_FAILED:"):] for f in failures]
            return False, "UNREACHABLE_URLS:" + "|".join(failed_urls), errors
        # Replace any FETCH_FAILED entries with readable text for mixed failure messages
        readable = [
            f"Could not fetch: {f[len('FETCH_FAILED:'):]}" if f.startswith("FETCH_FAILED:") else f
            for f in failures
        ]
        return False, " | ".join(readable), errors
    return True, "", errors


def _build_feedback_block(item: dict) -> str:
    """
    Build the feedback section injected into a generation prompt on retry.
    Distinguishes URL failures (cite different docs) from factual failures
    (the verifier found the answer was wrong — surface the exact correction).
    """
    feedback = item.get("feedback", "")
    ftype    = item.get("failure_type", "")
    attempt  = item.get("attempts", 0)

    if not feedback:
        return ""

    if ftype == "unreachable_url":
        # Message already constructed with bad URL list in the result loop
        return f"\n{feedback}\n"

    # Factual failure (or unclassified — treat as factual to be safe)
    base = (
        f"\nCRITICAL — YOUR PREVIOUS ANSWER WAS FACTUALLY INCORRECT:\n"
        f"The verifier checked your answer against Snowflake documentation and found:\n"
        f"  {feedback}\n"
        f"You MUST correct this factual error. The correct answer in your new question "
        f"MUST align with what Snowflake documentation actually states. "
        f"Do NOT reuse the same incorrect fact.\n"
    )
    if attempt >= 2:
        base += (
            f"\nThis concept has now failed verification {attempt} time(s). "
            f"Try a COMPLETELY DIFFERENT angle on this concept — if previous questions "
            f"focused on limits or behavior, try a scenario, an edge case, or a "
            f"'which of the following is NOT...' format instead.\n"
        )
    return base


def generate_question(concept: str, domain_name: str, variation: int = 0, feedback: str = "", existing_questions: list = None):
    """Generate a fresh exam question for the given concept via Cortex."""
    feedback_block = _build_feedback_block({"feedback": feedback, "failure_type": "factual" if feedback else "", "attempts": 0})

    variation_hint = (
        "\nThis is an ADDITIONAL question about this same concept. "
        "You MUST use a different question format, phrasing, and distractor set than you would normally choose. "
        "Vary the style — e.g. scenario-based, elimination ('which is NOT...'), or multi-select "
        "rather than a plain definitional question.\n"
    ) if variation > 0 else ""

    avoid_block = ""
    if existing_questions:
        listed = "\n".join(f"- {q}" for q in existing_questions)
        avoid_block = (
            f"\nThe following question(s) about this concept already exist. "
            f"Do NOT generate a question that is the same or very similar to any of these:\n{listed}\n"
        )

    prompt = f"""You are an expert Snowflake certified professional and exam author with authoritative knowledge of the Snowflake AI Data Cloud platform. Your questions are used by candidates preparing for the SnowPro Core (COF-C03) certification exam.

Your PRIMARY obligation is FACTUAL ACCURACY. Every correct answer you write must exactly match current Snowflake documentation. Incorrect facts that reach the cache will mislead exam candidates — this is the worst possible outcome.

Your SECONDARY obligation is to cite only real, accessible Snowflake documentation URLs (docs.snowflake.com) that directly confirm the correct answer. Do NOT cite a URL unless you are highly confident it exists and contains content that directly supports the correct answer.

The SnowPro Core (COF-C03) exam covers these broad curriculum areas. Use this as context when calibrating question scope, difficulty, and relevance:
- Fundamentals and architecture: Snowflake's three-layer architecture, editions, compute/storage separation, cloud services layer
- Interfaces and tools: Snowsight, SnowSQL, Snowflake CLI, connectors, and Snowpark basics
- Security and governance: RBAC, DAC, authentication methods, network policies, masking and access policies, tagging, encryption, Trust Center
- Data loading and storage: stages, COPY INTO (loading and unloading), PUT/GET/LIST/VALIDATE, Snowpipe, Snowpipe Streaming, file formats, micro-partitions, clustering
- SQL objects, views, and data types: databases, schemas, tables (all types), standard/secure/materialized/recursive views, UDFs, UDTFs, stored procedures, sequences, VARIANT/OBJECT/ARRAY/geospatial types
- Performance and cost optimization: query profile, caching layers, spilling, warehouse scaling, multi-cluster, resource monitors, sampling methods
- Automation and pipelines: streams, tasks (including DAGs), Dynamic Tables, Snowpipe Streaming, Git integration
- Semi-structured and complex data: FLATTEN, LATERAL, unstructured data, directory tables, file URL functions
- Advanced platform features: Snowpark, Streamlit in Snowflake, Cortex AI functions, ML features, Iceberg tables

Questions are conceptual and scenario-based — they test understanding of how Snowflake works, not memorization of syntax or exact values.

Generate exactly ONE exam question for the concept below.

Concept to test: {concept}
Domain: {domain_name}
{variation_hint}{avoid_block}{feedback_block}
Requirements:
- Choose EITHER single-select (1 correct answer) OR multi-select (2-3 correct answers)
- Single-select: exactly 4 options; multi-select: 4-5 options; question must say "Select all that apply"
- IMPORTANT: Do NOT default to index 0 as the correct answer. Vary the correct answer position across options (0, 1, 2, or 3).
- Distractors must represent specific, realistic Snowflake misconceptions — e.g. confusing which Edition requires a feature, mixing up similar feature names, or citing a value that is off by one category. Avoid generic or obviously silly wrong answers.
- Vary style: definitional, scenario-based, or elimination ("which is NOT...")
- Explanation must say why each correct answer is right AND why each wrong answer is wrong
- Include 1-2 citations to REAL Snowflake documentation pages. Prefer: docs.snowflake.com/en/user-guide/[feature], docs.snowflake.com/en/sql-reference/[command]. Avoid community articles or version-specific release notes.
- CITATION-CLAIM ALIGNMENT (critical): Your correct answer and explanation must assert ONLY facts that the specific page you cite EXPLICITLY states. Do NOT rely on general knowledge or facts documented only on other pages. If you cannot cite a page that explicitly contains a specific detail (an exact number, edition, or behavior), either cite the precise page that does state it, or rewrite the correct answer to assert only what your cited page actually says. A merely topically-related citation is NOT sufficient — the verifier fetches your cited page and rejects any claim it does not explicitly support.
- CRITICAL: The question text must be self-contained. Every condition required to reach the correct answer must be explicitly stated in the question itself. If the correct answer depends on a condition (e.g. "no WAREHOUSE is specified", "the table is transient", "the user is ACCOUNTADMIN"), that condition MUST appear in the question scenario — not only in the answer choices.

Return ONLY valid JSON with no markdown fences:
{{
  "question": "...",
  "type": "single",
  "options": ["...", "...", "...", "..."],
  "answer": [0],
  "explanation": "...",
  "citations": [
    {{"title": "...", "url": "https://docs.snowflake.com/..."}}
  ]
}}"""

    raw = _call_cortex(prompt)
    if raw is None:
        return None
    return _parse_json(raw)


def generate_questions_batch(items: list) -> list:
    """
    Generate multiple exam questions in a single Cortex call.
    items: list of dicts with keys: concept, domain_name, variation, feedback, existing_questions
    Returns a list of the same length — None per slot that failed to generate or parse.
    """
    if not items:
        return []

    entries = []
    for i, item in enumerate(items, 1):
        feedback_block = _build_feedback_block(item)

        variation_hint = (
            "\nThis is an ADDITIONAL question about this concept. "
            "Use a different question format, style, and distractors than a first question.\n"
        ) if item.get("variation", 0) > 0 else ""

        avoid_block = ""
        if item.get("existing_questions"):
            listed = "\n".join(f"  - {q}" for q in item["existing_questions"])
            avoid_block = f"\nDo NOT generate a question similar to any of these existing ones:\n{listed}\n"

        entries.append(
            f"QUESTION {i}:\n"
            f"Concept: {item['concept']}\n"
            f"Domain: {item['domain_name']}"
            f"{variation_hint}{avoid_block}{feedback_block}"
        )

    combined = "\n\n---\n\n".join(entries)
    n = len(items)
    prompt = f"""You are an expert Snowflake certified professional and exam author with authoritative knowledge of the Snowflake AI Data Cloud platform. Your questions are used by candidates preparing for the SnowPro Core (COF-C03) certification exam.

Your PRIMARY obligation is FACTUAL ACCURACY. Every correct answer you write must exactly match current Snowflake documentation. Incorrect facts that reach the cache will mislead exam candidates — this is the worst possible outcome.

Your SECONDARY obligation is to cite only real, accessible Snowflake documentation URLs (docs.snowflake.com) that directly confirm the correct answer. Do NOT cite a URL unless you are highly confident it exists and contains content that directly supports the correct answer.

The SnowPro Core (COF-C03) exam covers these broad curriculum areas. Use this as context when calibrating question scope, difficulty, and relevance:
- Fundamentals and architecture: Snowflake's three-layer architecture, editions, compute/storage separation, cloud services layer
- Interfaces and tools: Snowsight, SnowSQL, Snowflake CLI, connectors, and Snowpark basics
- Security and governance: RBAC, DAC, authentication methods, network policies, masking and access policies, tagging, encryption, Trust Center
- Data loading and storage: stages, COPY INTO (loading and unloading), PUT/GET/LIST/VALIDATE, Snowpipe, Snowpipe Streaming, file formats, micro-partitions, clustering
- SQL objects, views, and data types: databases, schemas, tables (all types), standard/secure/materialized/recursive views, UDFs, UDTFs, stored procedures, sequences, VARIANT/OBJECT/ARRAY/geospatial types
- Performance and cost optimization: query profile, caching layers, spilling, warehouse scaling, multi-cluster, resource monitors, sampling methods
- Automation and pipelines: streams, tasks (including DAGs), Dynamic Tables, Snowpipe Streaming, Git integration
- Semi-structured and complex data: FLATTEN, LATERAL, unstructured data, directory tables, file URL functions
- Advanced platform features: Snowpark, Streamlit in Snowflake, Cortex AI functions, ML features, Iceberg tables

Questions are conceptual and scenario-based — they test understanding of how Snowflake works, not memorization of syntax or exact values.

Generate exactly ONE exam question for EACH of the {n} concepts listed below. Return a JSON ARRAY of exactly {n} objects in the same order — one per concept.

{combined}

Requirements for EACH question:
- Single-select (exactly 4 options) OR multi-select (4-5 options, question must say "Select all that apply")
- IMPORTANT: Vary which option index is the correct answer. Do NOT consistently use index 0. Distribute correct answers across all four positions (0, 1, 2, 3) across the questions you generate.
- Distractors must represent specific, realistic Snowflake misconceptions — e.g. confusing which Edition requires a feature, mixing up similar feature names (Snowpipe vs Snowpipe Streaming), or citing a value that is off by one category (90 days vs 1 day). Avoid generic or obviously silly wrong answers.
- Explanation says why each correct/wrong answer is correct/wrong, referencing the specific documented fact
- Include 1-2 citations to REAL Snowflake documentation pages. Prefer these URL patterns: docs.snowflake.com/en/user-guide/[feature], docs.snowflake.com/en/sql-reference/[command], docs.snowflake.com/en/release-notes. Avoid community articles, old Classic Console paths, or version-specific release note entries.
- CITATION-CLAIM ALIGNMENT (critical): Your correct answer and explanation must assert ONLY facts that the specific page you cite EXPLICITLY states. Do NOT rely on general knowledge or facts documented only on other pages. If you cannot cite a page that explicitly contains a specific detail (an exact number, edition, or behavior), either cite the precise page that does state it, or rewrite the correct answer to assert only what your cited page actually says. A merely topically-related citation is NOT sufficient — the verifier fetches your cited page and rejects any claim it does not explicitly support.
- CRITICAL: The question text must be self-contained. Every condition needed to reach the correct answer must appear explicitly in the question itself — not only in the answer choices.

Return ONLY a valid JSON array with no markdown fences:
[
  {{"question": "...", "type": "single", "options": ["...", "...", "...", "..."], "answer": [0], "explanation": "...", "citations": [{{"title": "...", "url": "https://docs.snowflake.com/..."}}]}},
  ...
]"""

    raw = _call_cortex(prompt)
    if raw is None:
        return [None] * n

    parsed = _parse_json(raw)
    if not isinstance(parsed, list):
        # Cortex returned a single dict or garbage
        return [None] * n

    # Pad or trim to exactly n slots
    result = list(parsed[:n])
    while len(result) < n:
        result.append(None)
    return result


def _generate_batch_threadsafe(items: list) -> tuple:
    """
    Thread-safe variant of generate_questions_batch.
    Returns (results: list, error: str | None).
    Never touches Streamlit state — safe to call from worker threads.
    """
    if not items:
        return [], None

    entries = []
    for i, item in enumerate(items, 1):
        feedback_block = _build_feedback_block(item)

        variation_hint = (
            "\nThis is an ADDITIONAL question about this concept. "
            "Use a different question format, style, and distractors than a first question.\n"
        ) if item.get("variation", 0) > 0 else ""

        avoid_block = ""
        if item.get("existing_questions"):
            listed = "\n".join(f"  - {q}" for q in item["existing_questions"])
            avoid_block = f"\nDo NOT generate a question similar to any of these existing ones:\n{listed}\n"

        entries.append(
            f"QUESTION {i}:\n"
            f"Concept: {item['concept']}\n"
            f"Domain: {item['domain_name']}"
            f"{variation_hint}{avoid_block}{feedback_block}"
        )

    combined = "\n\n---\n\n".join(entries)
    n = len(items)
    prompt = f"""You are an expert Snowflake certified professional and exam author with authoritative knowledge of the Snowflake AI Data Cloud platform. Your questions are used by candidates preparing for the SnowPro Core (COF-C03) certification exam.

Your PRIMARY obligation is FACTUAL ACCURACY. Every correct answer you write must exactly match current Snowflake documentation. Incorrect facts that reach the cache will mislead exam candidates — this is the worst possible outcome.

Your SECONDARY obligation is to cite only real, accessible Snowflake documentation URLs (docs.snowflake.com) that directly confirm the correct answer. Do NOT cite a URL unless you are highly confident it exists and contains content that directly supports the correct answer.

The SnowPro Core (COF-C03) exam covers these broad curriculum areas. Use this as context when calibrating question scope, difficulty, and relevance:
- Fundamentals and architecture: Snowflake's three-layer architecture, editions, compute/storage separation, cloud services layer
- Interfaces and tools: Snowsight, SnowSQL, Snowflake CLI, connectors, and Snowpark basics
- Security and governance: RBAC, DAC, authentication methods, network policies, masking and access policies, tagging, encryption, Trust Center
- Data loading and storage: stages, COPY INTO (loading and unloading), PUT/GET/LIST/VALIDATE, Snowpipe, Snowpipe Streaming, file formats, micro-partitions, clustering
- SQL objects, views, and data types: databases, schemas, tables (all types), standard/secure/materialized/recursive views, UDFs, UDTFs, stored procedures, sequences, VARIANT/OBJECT/ARRAY/geospatial types
- Performance and cost optimization: query profile, caching layers, spilling, warehouse scaling, multi-cluster, resource monitors, sampling methods
- Automation and pipelines: streams, tasks (including DAGs), Dynamic Tables, Snowpipe Streaming, Git integration
- Semi-structured and complex data: FLATTEN, LATERAL, unstructured data, directory tables, file URL functions
- Advanced platform features: Snowpark, Streamlit in Snowflake, Cortex AI functions, ML features, Iceberg tables

Questions are conceptual and scenario-based — they test understanding of how Snowflake works, not memorization of syntax or exact values.

Generate exactly ONE exam question for EACH of the {n} concepts listed below. Return a JSON ARRAY of exactly {n} objects in the same order — one per concept.

{combined}

Requirements for EACH question:
- Single-select (exactly 4 options) OR multi-select (4-5 options, question must say "Select all that apply")
- IMPORTANT: Vary which option index is the correct answer. Do NOT consistently use index 0. Distribute correct answers across all four positions (0, 1, 2, 3) across the questions you generate.
- Distractors must represent specific, realistic Snowflake misconceptions — e.g. confusing which Edition requires a feature, mixing up similar feature names (Snowpipe vs Snowpipe Streaming), or citing a value that is off by one category (90 days vs 1 day). Avoid generic or obviously silly wrong answers.
- Explanation says why each correct/wrong answer is correct/wrong, referencing the specific documented fact
- Include 1-2 citations to REAL Snowflake documentation pages. Prefer these URL patterns: docs.snowflake.com/en/user-guide/[feature], docs.snowflake.com/en/sql-reference/[command], docs.snowflake.com/en/release-notes. Avoid community articles, old Classic Console paths, or version-specific release note entries.
- CITATION-CLAIM ALIGNMENT (critical): Your correct answer and explanation must assert ONLY facts that the specific page you cite EXPLICITLY states. Do NOT rely on general knowledge or facts documented only on other pages. If you cannot cite a page that explicitly contains a specific detail (an exact number, edition, or behavior), either cite the precise page that does state it, or rewrite the correct answer to assert only what your cited page actually says. A merely topically-related citation is NOT sufficient — the verifier fetches your cited page and rejects any claim it does not explicitly support.
- CRITICAL: The question text must be self-contained. Every condition needed to reach the correct answer must appear explicitly in the question itself — not only in the answer choices.

Return ONLY a valid JSON array with no markdown fences:
[
  {{"question": "...", "type": "single", "options": ["...", "...", "...", "..."], "answer": [0], "explanation": "...", "citations": [{{"title": "...", "url": "https://docs.snowflake.com/..."}}]}},
  ...
]"""

    raw, err = _call_cortex_threadsafe(prompt, model=GEN_MODEL, bust_cache=True)
    if err or raw is None:
        return [None] * n, err or "Cortex returned None"

    parsed = _parse_json(raw)
    if isinstance(parsed, dict):
        parsed = [parsed]   # model collapsed the array to one object — salvage it
    if not isinstance(parsed, list):
        return [None] * n, "Response was not a JSON array"

    result = list(parsed[:n])
    while len(result) < n:
        result.append(None)
    return result, None


def _fetch_doc_content(url: str) -> str | None:
    """
    Fetch a documentation URL and return the article body as plain text.
    Skips nav/header/footer/sidebar/script/style elements.
    Returns None on HTTP error or network failure.
    """
    try:
        from urllib.request import urlopen, Request
        from html.parser import HTMLParser
        import re

        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                return None
            html = resp.read().decode("utf-8", errors="replace")

        class _BodyExtractor(HTMLParser):
            SKIP_TAGS = {"script", "style", "nav", "header", "footer",
                         "aside", "noscript", "form"}
            def __init__(self):
                super().__init__()
                self.parts = []
                self._skip_depth = 0
            def handle_starttag(self, tag, attrs):
                if tag in self.SKIP_TAGS:
                    self._skip_depth += 1
            def handle_endtag(self, tag):
                if tag in self.SKIP_TAGS and self._skip_depth > 0:
                    self._skip_depth -= 1
            def handle_data(self, data):
                if self._skip_depth == 0:
                    self.parts.append(data)

        ex = _BodyExtractor()
        ex.feed(html)
        text = re.sub(r'\s+', ' ', " ".join(ex.parts)).strip()
        return text if text else None
    except Exception:
        return None


def _fetch_docs_parallel(urls: list) -> dict:
    """Fetch multiple documentation URLs simultaneously. Returns {url: content_or_None}."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if not urls:
        return {}
    results = {}
    with ThreadPoolExecutor(max_workers=min(len(urls), 8)) as pool:
        future_to_url = {pool.submit(_fetch_doc_content, url): url for url in urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                results[url] = future.result()
            except Exception:
                results[url] = None
    return results


def verify_citations(question: dict, prefetched: dict = None) -> tuple:
    """
    For each citation: fetch the actual documentation page (or use pre-fetched content),
    then ask Cortex to verify the answer is factually supported.
    Checks ALL citations before returning so every failure is reported at once.
    prefetched: optional {url: content} dict from _fetch_docs_parallel; skips HTTP fetch when present.
    Returns (True, "") if all citations pass, (False, combined_reasons) otherwise.
    """
    citations = question.get("citations", [])
    if not citations:
        return False, "No citations provided"

    correct_options = [question["options"][i] for i in question["answer"]]
    question_text = question["question"]
    explanation = question.get("explanation", "")

    failures = []

    for c in citations:
        url = c.get("url", "")

        content = (prefetched or {}).get(url) if prefetched is not None else None
        if content is None:
            content = _fetch_doc_content(url)
        if content is None:
            failures.append(f"Could not fetch documentation page: {url}")
            continue

        prompt = f"""You are a Snowflake certification exam fact-checker. Your job is to verify FACTUAL ACCURACY, not just topical relevance.

Below is the text content retrieved from this documentation page:
URL: {url}
---
{content}
---

Exam question: {question_text}
Stated correct answer(s): {correct_options}
Explanation: {explanation}

Carefully check whether the stated correct answer is factually accurate according to this documentation. Pay close attention to:
- Specific numbers, sizes, thresholds, percentages, or time values (e.g. "100-250 MB", "10%", "90 days")
- Specific behavior descriptions (e.g. "continues loading", "skips the file", "creates a pointer")
- Edition or feature requirements

If the documentation states specific facts that DIFFER from what the answer claims — even if the page is generally relevant to the topic — the citation is invalid.

A citation is valid ONLY if:
- The documentation explicitly confirms or directly supports the specific claim in the correct answer, OR
- The documentation provides directly relevant context that is fully consistent with every specific claim in the answer

A citation is invalid if:
- The documentation states facts that contradict the answer (e.g. doc says 100-250 MB but answer claims >1 GB), OR
- The documentation is generally relevant but contains no content that actually supports the specific answer claims, OR
- The page has no meaningful relationship to the question topic

Return ONLY valid JSON with no markdown fences:
{{
  "supported": true,
  "reason": "One sentence citing the specific documentation text that confirms or contradicts the answer."
}}"""

        raw = _call_cortex(prompt)
        if raw is None:
            failures.append(f"Cortex verification call failed for {url}")
            continue

        result = _parse_json(raw)
        if result is None:
            failures.append(f"Could not parse verifier response for {url}")
            continue

        if not result.get("supported", False):
            reason = result.get("reason", "no reason given")
            failures.append(f"{url} — {reason}")

    if failures:
        return False, " | ".join(failures)
    return True, ""


def show_connection_warning_if_needed():
    """Show a banner if Snowflake is unreachable. Called at top of exam/study pages.
    Offers an inline reconnect so the user doesn't have to leave a test to recover."""
    if get_snowflake_connection() is None:
        st.warning(
            "Could not connect to Snowflake — using static question bank. "
            "Check your VPN or re-authenticate (`snow connection test`), then reconnect.",
            icon="⚠️"
        )
        c1, _ = st.columns([1, 2])
        with c1:
            if st.button("🔁 Reconnect to Snowflake", key="conn_warn_reconnect",
                         type="primary", use_container_width=True):
                # The cache_resource entry is holding a stale None; clear it and
                # force a fresh connect (re-auth) right here so the user can stay
                # on the current page instead of leaving to the pre-load screen.
                get_snowflake_connection.clear()
                with st.spinner("Reconnecting to Snowflake…"):
                    ok, err = _check_connection()
                if ok:
                    st.rerun()
                else:
                    st.error(f"Still can't reach Snowflake: {err or 'no connection configured'}. "
                             "Check VPN / auth, or confirm a Snowflake connection is configured "
                             "(SNOWFLAKE_CONNECTION_NAME or a default connection).")
        return True
    return False


def get_or_generate_question(concept_idx: int, variation: int = 0):
    """
    Return a verified question for (concept_idx, variation).
    Checks cache first - no Cortex calls if cached.
    On miss: generates and verifies up to MAX_GENERATION_ATTEMPTS.
    Returns None if all attempts are exhausted.
    """
    cache = st.session_state.generated_questions
    cache_key = (concept_idx, variation)
    if cache_key in cache:
        return cache[cache_key]

    # No Snowflake connection — use static bank immediately
    if get_snowflake_connection() is None or st.session_state.offline_mode:
        return get_fallback_question(concept_idx)

    concept_entry = CONCEPT_BANK[concept_idx % len(CONCEPT_BANK)]
    domain_name = DOMAINS[concept_entry["domain"]]["name"]
    last_failure_reason = ""
    last_generated_q = None      # track best candidate in case citations never pass
    last_verify_reason = ""

    # Collect all question texts the LLM must avoid — both cached variations of
    # this concept AND every previously failed attempt (persisted across retries).
    failed_key = (concept_idx, variation)
    failed_texts = st.session_state.get("failed_q_texts", {}).get(failed_key, [])
    existing_qs = [
        cache[(concept_idx, v)]["question"]
        for v in range(VARIATIONS_PER_CONCEPT)
        if (concept_idx, v) in cache and (concept_idx, v) != cache_key
    ] + failed_texts

    for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
        suffix = f"(attempt {attempt}/{MAX_GENERATION_ATTEMPTS})"
        with st.spinner(f"Generating question... {suffix}"):
            q = generate_question(concept_entry["concept"], domain_name, variation=variation,
                                  feedback=last_failure_reason, existing_questions=existing_qs)

        if q is None:
            log_error(f"Attempt {attempt}/{MAX_GENERATION_ATTEMPTS}: generation returned None for concept {concept_idx} (v{variation}) — JSON parse or Cortex call failed")
            continue

        with st.spinner(f"Verifying citations... {suffix}"):
            passed, verify_reason = verify_citations(q)

        if passed:
            q["domain"] = concept_entry["domain"]
            q["id"] = concept_idx
            cache[cache_key] = q
            save_questions_cache()
            if "failed_q_texts" in st.session_state:
                st.session_state.failed_q_texts.pop(failed_key, None)
            return q

        # Keep the last generated question as a fallback candidate, but only if
        # the verifier didn't say the answer itself contradicts the documentation.
        if "contradict" not in verify_reason.lower():
            last_generated_q = q
            last_verify_reason = verify_reason

        # Record this failed question text so future cycles avoid it
        if q.get("question"):
            st.session_state.failed_q_texts.setdefault(failed_key, []).append(q["question"])

        last_failure_reason = verify_reason
        log_error(
            f"Attempt {attempt}/{MAX_GENERATION_ATTEMPTS}: citation verification failed "
            f"for concept {concept_idx} (v{variation}). {verify_reason}"
        )

    # All citation-verified attempts exhausted.
    return None


def get_fallback_question(concept_idx: int) -> dict:
    """Return a static-bank question as a fallback."""
    q = QUESTION_BANK[concept_idx % len(QUESTION_BANK)].copy()
    q["citations"] = []
    return q


def md_safe(text) -> str:
    """Escape '$' so Streamlit markdown/LaTeX renders it literally.
    Snowflake metadata columns like METADATA$ACTION contain '$', and a pair of
    them elsewhere in the same string would otherwise be interpreted as inline
    LaTeX math (rendering the text between them as italics/equation)."""
    return str(text).replace("$", "\\$")


def _generation_in_progress() -> bool:
    """True when a bulk pre-load run is active OR paused-but-queued.
    Mirrors the pre-load page's `is_running` signal so destructive cache
    controls on other pages can disable themselves: clearing the cache while a
    run is queued would wipe questions it already produced, and because those
    items were dropped from the queue as they completed, they would NOT be
    regenerated when the run resumes."""
    return bool(st.session_state.get("bulk_items")) and not st.session_state.get("stop_bulk", False)


def render_explanation_box(explanation: str, citations: list):
    """Render the explanation box with optional verified citation links."""
    citations_html = ""
    if citations:
        items = "".join(
            f'<li><a href="{c["url"]}" target="_blank" '
            f'style="color:#29B5E8;">{c["title"]}</a></li>'
            for c in citations
        )
        citations_html = f"""
        <div style="margin-top:0.6rem; font-size:0.85rem; opacity:0.85;">
            📚 <strong>References</strong>
            <ul style="margin:0.3rem 0 0 0; padding-left:0; list-style-position:inside;">
                {items}
            </ul>
        </div>"""
    st.markdown(
        f'<div class="explanation-box">💡 {md_safe(explanation)}{citations_html}</div>',
        unsafe_allow_html=True
    )


def get_snowflake_css():
    return """
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

        .main .block-container {
            padding-top: 2rem;
            max-width: 1200px;
        }

        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #0D2B45 0%, #11567F 50%, #29B5E8 100%);
        }

        [data-testid="stSidebar"] > div > div > div > div > p,
        [data-testid="stSidebar"] .stMarkdown,
        [data-testid="stSidebar"] .stCaption,
        [data-testid="stSidebar"] span,
        [data-testid="stSidebar"] label {
            color: white !important;
        }

        [data-testid="stSidebar"] button[kind="secondary"] p,
        [data-testid="stSidebar"] button[kind="secondary"] span {
            color: white !important;
        }

        /* Left-align sidebar nav buttons. The button itself is fine, but Streamlit
           wraps the label in two nested flex containers (a div and a span) that each
           default to justify-content:center — that's what centers the text. Force
           flex-start on the button AND every inner wrapper. */
        [data-testid="stSidebar"] button[kind="secondary"],
        [data-testid="stSidebar"] button[kind="primary"],
        [data-testid="stSidebar"] button[kind="secondary"] div,
        [data-testid="stSidebar"] button[kind="primary"] div,
        [data-testid="stSidebar"] button[kind="secondary"] span,
        [data-testid="stSidebar"] button[kind="primary"] span {
            justify-content: flex-start !important;
            text-align: left !important;
        }

        .hero-header {
            background: linear-gradient(135deg, #0D2B45 0%, #11567F 40%, #29B5E8 100%);
            padding: 2.5rem 2rem;
            border-radius: 16px;
            color: white;
            text-align: center;
            margin-bottom: 2rem;
            box-shadow: 0 8px 32px rgba(41, 181, 232, 0.3);
        }

        .hero-header h1 {
            font-family: 'Inter', sans-serif;
            font-size: 2.4rem;
            font-weight: 800;
            margin: 0;
            letter-spacing: -0.5px;
        }

        .hero-header p {
            font-size: 1.1rem;
            opacity: 0.9;
            margin-top: 0.5rem;
            font-weight: 300;
        }

        .domain-card {
            background: var(--secondary-background-color);
            color: var(--text-color);
            border-radius: 12px;
            padding: 1.2rem;
            border-left: 4px solid #29B5E8;
            box-shadow: 0 2px 8px rgba(0,0,0,0.12);
            margin-bottom: 0.8rem;
            transition: transform 0.2s, box-shadow 0.2s;
        }

        .domain-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 16px rgba(41, 181, 232, 0.25);
        }

        .domain-card small {
            color: var(--text-color);
            opacity: 0.7;
        }

        .question-card {
            background: var(--secondary-background-color);
            color: var(--text-color);
            border-radius: 16px;
            padding: 2rem;
            box-shadow: 0 4px 24px rgba(0,0,0,0.12);
            border: 1px solid rgba(41, 181, 232, 0.3);
            margin: 1rem 0;
        }

        .question-card h3 {
            color: var(--text-color);
        }

        .question-card p {
            color: var(--text-color);
        }

        .score-display {
            background: linear-gradient(135deg, #0D2B45 0%, #11567F 100%);
            border-radius: 16px;
            padding: 2rem;
            color: white;
            text-align: center;
            box-shadow: 0 8px 32px rgba(13, 43, 69, 0.3);
        }

        .score-display h2 {
            font-size: 3.5rem;
            font-weight: 800;
            margin: 0;
            color: white;
        }

        .correct-answer {
            background-color: #1a4a2e;
            border: 1px solid #28a745;
            border-radius: 10px;
            padding: 1rem;
            margin: 0.5rem 0;
            color: #a8f0c0;
        }

        .wrong-answer {
            background-color: #4a1a1e;
            border: 1px solid #dc3545;
            border-radius: 10px;
            padding: 1rem;
            margin: 0.5rem 0;
            color: #f0a8b0;
        }

        .explanation-box {
            background: rgba(41, 181, 232, 0.12);
            border-left: 4px solid #29B5E8;
            border-radius: 0 10px 10px 0;
            padding: 1rem 1.2rem;
            margin-top: 0.8rem;
            font-size: 0.95rem;
            color: var(--text-color);
        }

        .stat-card {
            background: var(--secondary-background-color);
            border-radius: 12px;
            padding: 1.5rem;
            text-align: center;
            box-shadow: 0 2px 12px rgba(0,0,0,0.12);
            border-top: 3px solid #29B5E8;
        }

        .stat-card h3 {
            font-size: 2rem;
            font-weight: 700;
            color: #29B5E8;
            margin: 0;
        }

        .stat-card p {
            color: var(--text-color);
            opacity: 0.8;
            font-size: 0.85rem;
            margin: 0;
        }

        .progress-ring {
            background: conic-gradient(#29B5E8 var(--progress), rgba(128,128,128,0.3) 0);
            border-radius: 50%;
            width: 120px;
            height: 120px;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0 auto;
        }

        .progress-ring-inner {
            background: var(--secondary-background-color);
            border-radius: 50%;
            width: 96px;
            height: 96px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.5rem;
            font-weight: 700;
            color: #29B5E8;
        }

        .timer-display {
            font-size: 1.8rem;
            font-weight: 700;
            color: #29B5E8;
            font-family: 'Inter', monospace;
        }

        .flashcard {
            background: var(--secondary-background-color);
            border-radius: 16px;
            padding: 2rem;
            min-height: 200px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.12);
            border: 2px solid rgba(41, 181, 232, 0.3);
            display: flex;
            align-items: center;
            justify-content: center;
            text-align: center;
        }

        .flashcard-front {
            font-size: 1.2rem;
            font-weight: 500;
            color: var(--text-color);
            line-height: 1.6;
        }

        .flashcard-back {
            font-size: 1.05rem;
            color: var(--text-color);
            line-height: 1.5;
        }

        div[data-testid="stMetric"] {
            background: var(--secondary-background-color);
            border-radius: 10px;
            padding: 1rem;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            border-top: 3px solid #29B5E8;
        }

        .snowflake-badge {
            display: inline-block;
            background: linear-gradient(135deg, #29B5E8, #11567F);
            color: white;
            padding: 0.3rem 0.8rem;
            border-radius: 20px;
            font-size: 0.75rem;
            font-weight: 600;
            letter-spacing: 0.5px;
        }

        /* Answer option spacing and sizing */
        [data-testid="stRadio"] label,
        [data-testid="stCheckbox"] label {
            font-size: 1.05rem !important;
            line-height: 1.5 !important;
        }

        [data-testid="stRadio"] > div > label,
        [data-testid="stCheckbox"] {
            padding: 0.45rem 0 !important;
        }
    </style>
    """


FLASHCARDS = [
    {"front": "What are the 3 layers of Snowflake's architecture?", "back": "1. Cloud Services Layer (brain)\n2. Compute Layer (virtual warehouses)\n3. Storage Layer (centralized, columnar micro-partitions)", "domain": "1"},
    {"front": "What is a micro-partition?", "back": "Immutable, compressed, columnar storage unit of 50-500 MB uncompressed. Snowflake automatically organizes data into micro-partitions with rich metadata (min/max, distinct count, null count) for efficient pruning.", "domain": "1"},
    {"front": "Credits per hour by warehouse size?", "back": "XS=1, S=2, M=4, L=8, XL=16, 2XL=32, 3XL=64, 4XL=128, 5XL=256, 6XL=512. Each size doubles credits and compute resources.", "domain": "1"},
    {"front": "Snowflake editions (lowest to highest)?", "back": "Standard < Enterprise < Business Critical < VPS\n\nEnterprise adds: multi-cluster WH, 90-day TT, masking, row access policies, automatic clustering\nBusiness Critical adds: Tri-Secret Secure, failover/failback, HIPAA/PCI-DSS\nVPS adds: dedicated infrastructure\n\nNote: Database/share REPLICATION is available on ALL editions. Failover/failback requires Business Critical+.", "domain": "1"},
    {"front": "What are the 6 system-defined roles?", "back": "1. ACCOUNTADMIN (top-level, inherits BOTH SYSADMIN and SECURITYADMIN)\n2. SECURITYADMIN (grants, inherits USERADMIN)\n3. USERADMIN (users & roles)\n4. SYSADMIN (warehouses & databases)\n5. PUBLIC (base role, granted to all)\n6. ORGADMIN (organization-level ops across accounts)\n\nHierarchy BRANCHES: ACCOUNTADMIN inherits SYSADMIN and SECURITYADMIN separately. It is NOT a single chain.", "domain": "2"},
    {"front": "Time Travel vs Fail-safe?", "back": "Time Travel: Self-service, 0-90 days (edition dependent), query/restore historical data\nFail-safe: Snowflake-support only, 7 days after TT expires, last resort recovery\n\nTransient/Temp tables: 0-1 day TT, NO Fail-safe\nPermanent tables: 0-90 day TT + 7 day Fail-safe", "domain": "5"},
    {"front": "3 types of caching in Snowflake?", "back": "1. Result Cache: 24hrs, Cloud Services Layer, exact query match, FREE\n2. Local Disk Cache: warehouse SSD, raw data from micro-partitions\n3. Metadata Cache: Cloud Services, table stats, min/max values", "domain": "4"},
    {"front": "Scale UP vs Scale OUT?", "back": "Scale UP: Increase warehouse size (XS→L) for faster individual queries (more resources per query)\n\nScale OUT: Multi-cluster warehouses for higher concurrency (more simultaneous queries)\n\nKey exam distinction!", "domain": "4"},
    {"front": "3 stage types in Snowflake?", "back": "1. User Stage (@~): One per user, can't be altered/dropped\n2. Table Stage (@%table_name): One per table, can't be altered/dropped\n3. Named Stage (CREATE STAGE): Internal or external, most flexible", "domain": "3"},
    {"front": "COPY INTO best practices for file sizes?", "back": "Recommended: 100-250 MB compressed per file\nToo small = overhead from file management\nToo large = limits parallelism\nSnowflake can split large CSV/JSON files automatically", "domain": "3"},
    {"front": "Snowpipe vs COPY INTO?", "back": "Snowpipe: Continuous, serverless, event-driven (auto-ingest via cloud notifications), micro-batch\nCOPY INTO: Batch, user-initiated, uses warehouse compute, better for large bulk loads", "domain": "3"},
    {"front": "What is Zero-Copy Cloning?", "back": "Creates metadata pointer to existing micro-partitions (no data copied). Changes use copy-on-write. Nearly instant, minimal cost. Can clone: databases, schemas, tables, streams, sequences, file formats, tasks, stages. CANNOT clone: shares, warehouses.", "domain": "5"},
    {"front": "Secure Data Sharing key points?", "back": "- Live, read-only access (no data copied/moved)\n- Provider pays storage, Consumer pays compute\n- Works across accounts, regions, clouds\n- Reader Accounts for non-Snowflake users (provider pays compute)\n- Only secure views/UDFs can be shared (not regular views)", "domain": "5"},
    {"front": "VARIANT data type?", "back": "Primary type for semi-structured data (JSON, Avro, XML, etc.). Max 16 MB compressed per value. Access with dot notation (col:key) or bracket notation (col['key']). FLATTEN function converts nested data to rows.", "domain": "3"},
    {"front": "Resource Monitor key facts?", "back": "Controls credit usage at account or warehouse level.\nActions at thresholds: Notify, Suspend (finish running queries), Suspend Immediately\nOnly ACCOUNTADMIN can create account-level monitors.\nCan set monthly, weekly, daily, or custom intervals.", "domain": "2"},
    {"front": "INFORMATION_SCHEMA vs ACCOUNT_USAGE?", "back": "INFORMATION_SCHEMA: Real-time, 7-14 day retention, per-database, no latency\nACCOUNT_USAGE: Up to 365 days, account-wide, 45min-3hr latency, in SNOWFLAKE database\n\nACCOUNT_USAGE requires IMPORTED PRIVILEGES on SNOWFLAKE database.", "domain": "2"},
    {"front": "Dynamic Data Masking?", "back": "Column-level security that masks data at query time based on the querying user's role. Enterprise+. Example: Full SSN for ADMIN, masked for ANALYST. Applied via masking policy on column.", "domain": "2"},
    {"front": "Clustering Keys?", "back": "Define how data is organized within micro-partitions for better pruning. NOT indexes! Enterprise+. Best for large tables (multi-TB) with known filter columns. Automatic Clustering maintains the clustering over time.", "domain": "4"},
    {"front": "Streams & Tasks?", "back": "Streams: CDC (Change Data Capture) on tables - track inserts, updates, deletes\nTasks: Scheduled SQL/stored procedures (cron or interval)\nCommon pattern: Stream detects changes → Task processes them (ETL pipeline)", "domain": "3"},
    {"front": "Cloud Services billing?", "back": "Free up to 10% of daily warehouse compute credits. Only amount exceeding 10% is billed. This means most customers pay $0 for cloud services. Heavy metadata operations can push usage over 10%.", "domain": "1"},
    {"front": "Apache Iceberg Tables?", "back": "Open table format managed by Snowflake. Enables interoperability with Spark, Flink, etc. Snowflake handles compaction, optimization. Created with CREATE ICEBERG TABLE. Supports external volumes for storage.", "domain": "1"},
    {"front": "Snowflake Notebooks?", "back": "Native interactive development in Snowsight. Supports SQL, Python, Markdown cells. Run on Snowflake compute (warehouse or Snowpark Container Services). Used for data exploration, ML, collaboration.", "domain": "1"},
    {"front": "Snowflake Cortex AI Functions?", "back": "AI SQL functions: COMPLETE (LLM), SUMMARIZE, CLASSIFY, EXTRACT, SENTIMENT, TRANSLATE, EMBED\nCortex Search: hybrid keyword+semantic search\nCortex Analyst: natural language text-to-SQL\nAll fully managed, run inside Snowflake.", "domain": "1"},
    {"front": "Dynamic Tables vs Streams+Tasks?", "back": "Dynamic Tables: Declarative (just define query + TARGET_LAG), Snowflake manages refresh\nStreams+Tasks: Imperative (you manage CDC logic + scheduling)\n\nDynamic Tables are simpler for most pipeline use cases. Both are in Domain 3.", "domain": "3"},
    {"front": "Snowpipe vs Snowpipe Streaming?", "back": "Snowpipe: File-based, stages files then loads, seconds-to-minutes latency, serverless\nSnowpipe Streaming: Row-based via SDK, no file staging, sub-second latency, uses Ingest SDK\n\nBoth are serverless. Streaming is lower latency.", "domain": "3"},
    {"front": "Data Clean Rooms?", "back": "Secure multi-party data collaboration. Parties can run approved analyses on combined datasets without seeing each other's raw data. Use cases: advertising measurement, healthcare research, financial compliance.", "domain": "5"},
    {"front": "Native Apps Framework?", "back": "Build & distribute apps via Marketplace. Includes: Application Package (code, data, UI), Consumer installs in their account. Can include Streamlit UI, stored procs, UDFs, shared data. Supports monetization.", "domain": "5"},
    {"front": "Trust Center?", "back": "Snowsight security feature. Provides: CIS benchmark compliance, security recommendations, threat detection, risk assessments. Helps admins find/fix security vulnerabilities. Account-level security posture.", "domain": "2"},
    {"front": "Git Integration in Snowflake?", "back": "Connect Snowflake to Git repos (GitHub, GitLab). CREATE GIT REPOSITORY stage. Sync version-controlled code (SPs, UDFs) into Snowflake. Enables CI/CD workflows for Snowflake objects.", "domain": "3"},
    {"front": "3 types of URLs for unstructured data?", "back": "1. Scoped URL (BUILD_SCOPED_FILE_URL): 24hr, user-specific, audit logged\n2. File URL (BUILD_STAGE_FILE_URL): permanent, needs stage privileges\n3. Pre-signed URL (GET_PRESIGNED_URL): time-limited, open access via HTTPS\n\nFiles stored in stages (internal or external). Directory tables catalog staged files.", "domain": "1"},
    {"front": "Snowflake drivers & connectors?", "back": "JDBC (Java), ODBC (C/C++), Python Connector, Node.js, Go, .NET, PHP PDO\n\nAll use HTTPS with TLS 1.2+\nSnowSQL = CLI for SQL queries\nSnowflake CLI (snow) = modern tool for apps, Streamlit, Snowpark\n\nBI tools (Tableau, Power BI) use ODBC/JDBC or native connectors.", "domain": "3"},
    {"front": "Serverless features in Snowflake?", "back": "No user-managed warehouse needed:\n- Snowpipe (continuous ingest)\n- Automatic Clustering\n- Search Optimization Service\n- Tasks (serverless mode)\n- Query Acceleration Service\n- Materialized View maintenance\n\nAll billed via serverless credit model.", "domain": "1"},
    {"front": "Semi-structured data types?", "back": "VARIANT: General container for any semi-structured value (JSON, Avro, Parquet, ORC, XML). Max 16 MB compressed.\nOBJECT: Key-value pairs (like JSON object)\nARRAY: Ordered list of values\n\nAccess with: dot notation (col:key), bracket notation (col['key']), FLATTEN for arrays.", "domain": "1"},
    {"front": "Projection Policy vs Masking Policy?", "back": "Masking Policy: Returns masked/redacted value (e.g., '***' instead of SSN). Column appears in results but data is hidden.\n\nProjection Policy: Blocks the column entirely from SELECT results. Query fails if column is projected unless role is allowed.\n\nBoth are column-level governance. Enterprise edition required.", "domain": "2"},
    {"front": "Data Classification (SYSTEM$CLASSIFY)?", "back": "Automated process to identify sensitive data:\n- Analyzes column data + metadata\n- Detects PII (name, email, phone, SSN)\n- Assigns system tags for governance\n- Can be manual or automatic\n\nUse: CALL SYSTEM$CLASSIFY('table_name')\nResults show semantic category and privacy category per column.", "domain": "2"},
    {"front": "IMPORTED PRIVILEGES on SNOWFLAKE db?", "back": "The SNOWFLAKE database is a system-shared database containing:\n- ACCOUNT_USAGE (detailed, 45-min latency)\n- INFORMATION_SCHEMA (real-time, db-level)\n- ORGANIZATION_USAGE (org-level metrics)\n\nGrant access: GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE my_role;", "domain": "2"},
    {"front": "Query result cache rules?", "back": "- Persists 24 hours in Cloud Services layer\n- Zero compute cost for cache hits\n- Invalidated by any DML on underlying tables\n- Only for deterministic queries (no CURRENT_TIMESTAMP, RANDOM, etc.)\n- Must be exact same SQL text\n- User must have access to all referenced objects", "domain": "4"},
    {"front": "External Tables?", "back": "Read-only tables referencing data in external cloud storage (S3, Azure, GCS).\n- Data stays in customer storage (not loaded)\n- Can have auto-refresh via cloud event notifications\n- Support partitioning for pruning\n- Metadata stored in Snowflake, data in cloud\n- Use for data lake query patterns.", "domain": "1"},
    {"front": "Warehouse resize behavior?", "back": "Resize up (scale up): More resources per query\nResize down (scale down): Fewer resources, lower cost\n\nKey: Running queries finish with original size. Only NEW queries use new size.\n\nMulti-cluster (scale out): Adds/removes clusters for concurrency. Enterprise+ only.", "domain": "4"},
]


def init_session_state():
    saved = load_progress()
    defaults = {
        "mode": "home",
        "exam_questions": [],
        "exam_answers": {},
        "exam_submitted": False,
        "exam_start_time": None,
        "exam_current_q": 0,
        "study_domain": None,
        "study_q_index": 0,
        "study_show_answer": False,
        "flash_index": 0,
        "flash_flipped": False,
        "flash_domain": "all",
        "history": [],
        "total_questions_answered": saved.get("total_questions_answered", 0),
        "total_correct": saved.get("total_correct", 0),
        "domain_stats_study": saved.get("domain_stats_study", {d: {"correct": 0, "total": 0} for d in DOMAINS}),
        "domain_stats_exam": saved.get("domain_stats_exam", {d: {"correct": 0, "total": 0} for d in DOMAINS}),
        "exam_history": saved.get("exam_history", []),
        "generated_questions": load_questions_cache(),
        "error_log": [],
        "error_unseen_count": 0,
        "error_log_open": False,
        "generation_paused": False,
        "error_context": "",
        "gen_retries": {},
        "failed_q_texts": {},
        "bulk_items": [],           # [{concept_idx, variation, attempts, feedback, failed_texts}]
        "bulk_total": 0,            # total items when generation started (for progress bar)
        "bulk_batch_size": BULK_BATCH_SIZE,
        "stop_bulk": False,
        "bulk_critical_error": None,  # set when a run is aborted by an auth/session failure
        "offline_mode": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def render_sidebar():
    with st.sidebar:
        st.markdown('<div style="text-align:center; padding: 1rem 0;"><span style="font-size: 3rem;">❄️</span></div>', unsafe_allow_html=True)
        st.markdown('<div style="text-align:center; font-size:1.3rem; font-weight:700; margin-bottom:0.2rem;">SnowPro Core</div>', unsafe_allow_html=True)
        st.markdown('<div style="text-align:center; font-size:0.85rem; opacity:0.8; margin-bottom:1.5rem;">COF-C03 Exam Prep</div>', unsafe_allow_html=True)
        if st.session_state.get("offline_mode"):
            st.markdown('<div style="text-align:center; font-size:0.75rem; background:rgba(255,200,0,0.25); border-radius:6px; padding:3px 8px; margin-bottom:0.5rem; color:white;">📴 Offline Mode</div>', unsafe_allow_html=True)
        st.divider()

        if st.button("🏠 Home", use_container_width=True, key="nav_home"):
            st.session_state.mode = "home"
            st.rerun()
        if st.button("🎓 Practice Exam", use_container_width=True, key="nav_exam"):
            st.session_state.mode = "exam_setup"
            st.rerun()
        if st.button("🃏 Flashcards", use_container_width=True, key="nav_flash"):
            st.session_state.mode = "flashcards"
            st.rerun()
        if st.button("📝 Study by Domain", use_container_width=True, key="nav_study"):
            st.session_state.mode = "study_select"
            st.rerun()
        if st.button("📈 Progress", use_container_width=True, key="nav_progress"):
            st.session_state.mode = "progress"
            st.rerun()
        if st.button("📖 Quick Reference", use_container_width=True, key="nav_ref"):
            st.session_state.mode = "reference"
            st.rerun()
        bulk_label = "⚡ Pre-load Questions"
        if st.session_state.bulk_items:
            n_left = len(st.session_state.bulk_items)
            bulk_label = f"⚡ Pre-loading ({n_left} remaining)..."
        if st.button(bulk_label, use_container_width=True, key="nav_preload"):
            st.session_state.mode = "preload"
            st.rerun()

        st.divider()

        history = st.session_state.exam_history
        if history:
            last = history[-1]
            exam_icon = "✅" if last["passed"] else "❌"
            exam_val = f"{exam_icon} {last['score']}/1000 <span style='opacity:0.6;font-size:0.75rem'>{last['date']}</span>"
        else:
            exam_val = "<span style='opacity:0.6'>No exams taken yet</span>"

        total = st.session_state.total_questions_answered
        correct = st.session_state.total_correct
        pct = int((correct / total * 100)) if total > 0 else 0
        study_val = f"{correct}/{total} correct ({pct}%)" if total > 0 else "<span style='opacity:0.6'>No questions yet</span>"

        st.markdown(f"""
        <div style="color:white; padding: 0.1rem 0;">
            <div style="background:rgba(255,255,255,0.1); border-radius:10px; padding:0.7rem 0.9rem; margin-bottom:0.7rem;">
                <div style="font-size:1rem; font-weight:700; margin-bottom:4px;">🎓 Last practice exam</div>
                <ul style="margin:0; padding-left:0; list-style-position:inside; font-size:0.95rem; font-weight:400; opacity:0.9;">
                    <li style="margin-bottom:0;">{exam_val}</li>
                </ul>
            </div>
            <div style="background:rgba(255,255,255,0.1); border-radius:10px; padding:0.7rem 0.9rem;">
                <div style="font-size:1rem; font-weight:700; margin-bottom:4px;">📝 Study accuracy</div>
                <ul style="margin:0; padding-left:0; list-style-position:inside; font-size:0.95rem; font-weight:400; opacity:0.9;">
                    <li style="margin-bottom:0;">{study_val}</li>
                </ul>
            </div>
        </div>
        """, unsafe_allow_html=True)


def render_error_log():
    """Fully independent error log section, always at the bottom of every page."""
    errors = st.session_state.error_log
    if not errors:
        return

    st.divider()

    unseen = st.session_state.error_unseen_count
    indicator = "🔴" if unseen > 0 else "⚪"
    new_label = f", {unseen} new" if unseen > 0 else ""
    header = f"{indicator} Generation Errors ({len(errors)} total{new_label})"

    # Use a toggle button to drive open/close — persists across reruns unlike st.expander
    if st.button(header, key="error_log_toggle"):
        st.session_state.error_log_open = not st.session_state.error_log_open
        if st.session_state.error_log_open:
            st.session_state.error_unseen_count = 0

    if st.session_state.error_log_open:
        entries_html = "".join(
            f"<p style='font-family:monospace; font-size:0.8rem; margin:0.3rem 0; "
            f"word-break:break-word;'><code>{entry['time']}</code> {entry['message']}</p>"
            for entry in reversed(errors[-50:])
        )
        st.markdown(
            f"<div style='background:rgba(220,53,69,0.08); border:1px solid rgba(220,53,69,0.3); "
            f"border-radius:10px; padding:1rem; margin-top:0.5rem;'>{entries_html}</div>",
            unsafe_allow_html=True
        )


def render_preload():
    """Bulk pre-generate and cache questions before a timed exam."""
    st.markdown(get_snowflake_css(), unsafe_allow_html=True)
    st.markdown("""
    <div class="hero-header">
        <h1>⚡ Pre-load Question Cache</h1>
        <p>Generate and cache questions now so the exam runs without interruption.</p>
    </div>
    """, unsafe_allow_html=True)

    # --- Critical-error recovery banner ---
    # Set when a run was aborted (or blocked pre-flight) by an expired token /
    # dropped session. The connection is cached via @st.cache_resource, so we must
    # clear it to force a fresh re-authentication.
    if st.session_state.get("bulk_critical_error"):
        st.error(
            "**Snowflake session expired.** The generation run was stopped so it "
            "wouldn't keep failing. Click **Reconnect**, then use **Generate Missing "
            "Questions** to continue where it left off — already-cached questions are kept."
        )
        st.caption(f"Details: {st.session_state.bulk_critical_error}")
        bc1, bc2, _ = st.columns([1, 1, 3])
        with bc1:
            if st.button("🔁 Reconnect to Snowflake", type="primary", use_container_width=True):
                # Evict the stale cached connection, then immediately establish a
                # fresh one so re-authentication (e.g. externalbrowser) happens NOW,
                # on button press — not lazily on the next Cortex call. Verify the
                # new session before clearing the banner.
                get_snowflake_connection.clear()
                with st.spinner("Reconnecting to Snowflake…"):
                    ok, err = _check_connection()
                if ok:
                    st.session_state.bulk_critical_error = None
                else:
                    st.session_state.bulk_critical_error = f"Reconnect failed: {err}"
                st.rerun()
        with bc2:
            if st.button("Dismiss", use_container_width=True):
                st.session_state.bulk_critical_error = None
                st.rerun()

    cache = st.session_state.generated_questions
    total_concepts = len(CONCEPT_BANK)
    total_all = total_concepts * VARIATIONS_PER_CONCEPT
    total_exam = total_concepts  # variation 0 only

    # --- Cache status table ---
    # Rendered into a placeholder via a helper so it can refresh live as
    # questions pass verification during an in-flight batch (not just per rerun).
    cache_status_ph = st.empty()

    def _render_cache_status():
        with cache_status_ph.container():
            rows = []
            for d_id, d_info in DOMAINS.items():
                indices = [i for i, c in enumerate(CONCEPT_BANK) if c["domain"] == d_id]
                d_total = len(indices) * VARIATIONS_PER_CONCEPT
                d_cached = sum(1 for i in indices for v in range(VARIATIONS_PER_CONCEPT) if (i, v) in cache)
                pct = int(d_cached / d_total * 100) if d_total else 0
                rows.append((f"{d_info['icon']} Domain {d_id}: {d_info['name']}", d_cached, d_total, pct))

            overall_cached = len(cache)
            exam_cached = sum(1 for i in range(total_concepts) if (i, 0) in cache)

            col_h, col_c, col_t = st.columns([4, 1, 1])
            col_h.markdown("**Domain**")
            col_c.markdown("**Cached**")
            col_t.markdown("**Total**")
            st.divider()
            for name, cached, total, pct in rows:
                c1, c2, c3 = st.columns([4, 1, 1])
                c1.write(name)
                c2.write(f"{cached}/{total}")
                status = "✅" if cached == total else ("🟡" if cached > 0 else "⭕")
                c3.write(f"{status} {pct}%")
            st.divider()
            st.write(f"**Exam-ready (variation 0):** {exam_cached}/{total_exam}   |   "
                     f"**All questions:** {overall_cached}/{total_all}")
            st.caption(f"Timing log: `{TIMING_LOG_FILE}`")

    _render_cache_status()

    st.write("")

    def _preflight_or_abort():
        """Verify the token/session before starting a token-requiring run. On
        failure, record the error and rerun to show the reconnect banner.
        st.rerun() raises, so code after this call runs only when healthy."""
        ok, err = _check_connection()
        if not ok:
            st.session_state.bulk_critical_error = err
            st.rerun()

    def _abort_run_critical(raw_err: str):
        """A run hit an auth/session failure mid-flight. Stop cleanly and show the
        reconnect banner instead of flooding the log with one error per parallel
        call across every remaining batch. Logs once; st.rerun() raises."""
        log_error(f"Generation aborted — Snowflake session/authentication failure: {raw_err}")
        st.session_state.bulk_critical_error = raw_err
        st.session_state.bulk_items = []
        st.session_state.bulk_total = 0
        st.session_state.stop_bulk = False
        st.rerun()

    # --- Generation buttons ---
    bulk_items = st.session_state.bulk_items
    is_running = bool(bulk_items) and not st.session_state.stop_bulk

    if not is_running:
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Generate Exam Questions", type="primary", use_container_width=True,
                         help="Generates variation 0 for every concept — enough for any timed exam"):
                _preflight_or_abort()
                items = [
                    {"concept_idx": i, "variation": 0, "attempts": 0, "feedback": "", "failure_type": "", "failed_texts": []}
                    for i in range(total_concepts)
                    if (i, 0) not in cache
                ]
                if items:
                    st.session_state.bulk_items = items
                    st.session_state.bulk_total = len(items)
                    st.session_state.stop_bulk  = False
                    st.rerun()
                else:
                    st.success("All exam questions are already cached!")
        with col2:
            if st.button("Generate Missing Questions", use_container_width=True,
                         help="Generates any not-yet-cached questions across both variations "
                              "(full study + exam coverage). Skips anything already cached."):
                _preflight_or_abort()
                items = [
                    {"concept_idx": i, "variation": v, "attempts": 0, "feedback": "", "failure_type": "", "failed_texts": []}
                    for i in range(total_concepts)
                    for v in range(VARIATIONS_PER_CONCEPT)
                    if (i, v) not in cache
                ]
                if items:
                    st.session_state.bulk_items = items
                    st.session_state.bulk_total = len(items)
                    st.session_state.stop_bulk  = False
                    st.rerun()
                else:
                    st.success("All questions are already cached!")

    # Reset/regenerate controls are only offered when idle. While a load is
    # running, the ONLY available action is "Stop" (rendered in the batch loop
    # below) — otherwise users could wipe the cache mid-generation.
    if not is_running:
        st.divider()
        st.write("**Reset & regenerate** — clears the question cache first, then generates everything fresh.")
        rc1, rc2 = st.columns(2)
        with rc1:
            if st.button("↺ Reset & Regenerate Exam Questions", use_container_width=True,
                         help="Clears all cached questions, then generates variation 0 for every concept"):
                _preflight_or_abort()   # verify token BEFORE wiping the cache
                st.session_state.generated_questions = {}
                if os.path.exists(QUESTIONS_CACHE_FILE):
                    os.remove(QUESTIONS_CACHE_FILE)
                items = [
                    {"concept_idx": i, "variation": 0, "attempts": 0, "feedback": "", "failure_type": "", "failed_texts": []}
                    for i in range(total_concepts)
                ]
                st.session_state.bulk_items = items
                st.session_state.bulk_total = len(items)
                st.session_state.stop_bulk  = False
                st.rerun()
        with rc2:
            if st.button("↺ Reset & Regenerate All Questions", use_container_width=True,
                         help="Clears all cached questions, then generates all variations for every concept"):
                _preflight_or_abort()   # verify token BEFORE wiping the cache
                st.session_state.generated_questions = {}
                if os.path.exists(QUESTIONS_CACHE_FILE):
                    os.remove(QUESTIONS_CACHE_FILE)
                items = [
                    {"concept_idx": i, "variation": v, "attempts": 0, "feedback": "", "failure_type": "", "failed_texts": []}
                    for i in range(total_concepts)
                    for v in range(VARIATIONS_PER_CONCEPT)
                ]
                st.session_state.bulk_items = items
                st.session_state.bulk_total = len(items)
                st.session_state.stop_bulk  = False
                st.rerun()

    # --- Active batch generation loop (one batch per rerun) ---
    if bulk_items and not st.session_state.stop_bulk:
        batch_size   = st.session_state.bulk_batch_size
        n_parallel   = PARALLEL_GEN_BATCHES
        # Grab up to batch_size * n_parallel items so we can fire N Cortex calls in parallel
        extended_batch = bulk_items[:batch_size * n_parallel]
        remaining      = bulk_items[batch_size * n_parallel:]
        total_orig  = st.session_state.bulk_total or len(bulk_items)
        done_so_far = total_orig - len(bulk_items)

        # Progress display
        progress_pct = done_so_far / total_orig if total_orig else 0
        first_concept = CONCEPT_BANK[extended_batch[0]["concept_idx"]]["concept"]
        st.progress(progress_pct,
                    text=f"Batch {done_so_far // batch_size + 1} — "
                         f"generating {len(extended_batch)} questions starting with: {first_concept}")
        if st.button("Stop", key="bulk_stop", type="secondary"):
            st.session_state.stop_bulk = True
            st.rerun()

        # Live within-batch status bar — updated as gen/fetch/verify futures complete
        phase_status = st.empty()

        batch_start   = time.time()
        batch_num     = done_so_far // batch_size + 1
        total_batches = (total_orig + batch_size - 1) // batch_size
        concept_ids   = [item["concept_idx"] for item in extended_batch]
        log_ctx       = f"BATCH {batch_num}/{total_batches} | concepts {concept_ids}"
        write_timing_log(log_ctx, "start",
            f"batch_size={len(extended_batch)} ({n_parallel}x{batch_size} parallel), pending_after={len(remaining)}")

        # Build generation inputs, split into sub-batches for parallel generation
        def _build_gen_inputs(items):
            inputs = []
            for item in items:
                c = CONCEPT_BANK[item["concept_idx"]]
                existing = [
                    cache[(item["concept_idx"], v)]["question"]
                    for v in range(VARIATIONS_PER_CONCEPT)
                    if (item["concept_idx"], v) in cache
                ] + item["failed_texts"]
                inputs.append({
                    "concept":            c["concept"],
                    "domain_name":        DOMAINS[c["domain"]]["name"],
                    "variation":          item["variation"],
                    "feedback":           item["feedback"],
                    "existing_questions": existing,
                })
            return inputs

        sub_batches = [
            extended_batch[i:i + batch_size]
            for i in range(0, len(extended_batch), batch_size)
        ]
        sub_inputs = [_build_gen_inputs(sb) for sb in sub_batches]

        # 1. Generate all sub-batches in parallel. Consume futures as they complete
        #    (not pool.map, which blocks until ALL finish) so we can show live progress.
        _t_gen = time.time()
        from concurrent.futures import ThreadPoolExecutor, as_completed
        sub_results = [None] * len(sub_inputs)
        gen_done = 0
        with ThreadPoolExecutor(max_workers=len(sub_batches)) as gen_pool:
            fut_to_idx = {
                gen_pool.submit(_generate_batch_threadsafe, inp): idx
                for idx, inp in enumerate(sub_inputs)
            }
            for fut in as_completed(fut_to_idx):
                sub_results[fut_to_idx[fut]] = fut.result()   # preserve input order by index
                gen_done += 1
                phase_status.progress(
                    gen_done / len(sub_inputs),
                    text=f"🧠 Generating — {gen_done}/{len(sub_inputs)} parallel batches complete")
        _gen_sec = time.time() - _t_gen

        # Flatten results; collect any gen errors for logging on the main thread
        generated = []
        gen_errors = []
        for (results, err) in sub_results:
            generated.extend(results)
            if err:
                gen_errors.append(err)
        _critical = next((e for e in gen_errors if _is_critical_error(e)), None)
        if _critical:
            _abort_run_critical(_critical)   # stops the run; st.rerun() raises
        for e in gen_errors:
            log_error(f"Parallel generation error: {e}")

        # Use extended_batch as "batch" for the rest of the pipeline
        batch = extended_batch

        _gen_ok = sum(1 for q in generated if q is not None)
        write_timing_log(log_ctx, "gen",
            f"{len(batch)} q requested | {_gen_ok} parsed ok | {_gen_sec:.2f}s "
            f"({_gen_sec / max(_gen_ok, 1):.2f}s/q) | {len(sub_batches)} parallel calls")

        # 2. Collect all citation URLs and fetch in parallel
        all_urls = list({
            cit["url"]
            for q in generated if q
            for cit in (q.get("citations") or [])
            if cit.get("url")
        })
        prefetched = {}
        _t_fetch = time.time()
        if all_urls:
            phase_status.info(f"🌐 Fetching {len(all_urls)} citation pages in parallel…")
            prefetched = _fetch_docs_parallel(all_urls)
        _fetch_sec = time.time() - _t_fetch
        _fetched_ok = sum(1 for v in prefetched.values() if v)
        write_timing_log(log_ctx, "fetch",
            f"{len(all_urls)} URLs | {_fetched_ok} fetched ok | {_fetch_sec:.2f}s (parallel)")

        # 3. Verify all generated questions in parallel (thread-safe, no Streamlit calls in workers)
        still_pending = []
        valid_pairs = [(item, q) for item, q in zip(batch, generated) if q is not None]
        null_pairs  = [(item, q) for item, q in zip(batch, generated) if q is None]

        # Handle generation failures (q is None)
        for item, _ in null_pairs:
            item["attempts"] += 1
            if item["attempts"] < MAX_GENERATION_ATTEMPTS:
                still_pending.append(item)
            else:
                log_error(f"Concept {item['concept_idx']} (v{item['variation']}) exhausted all batch attempts — generation returned None")

        # Verify valid questions in parallel, updating a live counter as each finishes
        _t_verify = time.time()
        if valid_pairs:
            future_to_pair = {}
            verify_results = {}
            v_done = 0
            v_pass_live = 0
            with ThreadPoolExecutor(max_workers=min(len(valid_pairs), 12)) as pool:
                for item, q in valid_pairs:
                    fut = pool.submit(_verify_question_threadsafe, q, prefetched)
                    future_to_pair[fut] = (item, q)
                for fut in as_completed(future_to_pair):
                    res = fut.result()
                    verify_results[fut] = res
                    v_done += 1
                    if res[0]:   # passed — cache immediately so counts update live
                        v_pass_live += 1
                        item, q = future_to_pair[fut]
                        q["domain"] = CONCEPT_BANK[item["concept_idx"]]["domain"]
                        q["id"]     = item["concept_idx"]
                        cache[(item["concept_idx"], item["variation"])] = q
                        _render_cache_status()   # live count refresh mid-flight
                    phase_status.progress(
                        v_done / len(valid_pairs),
                        text=f"🔍 Verifying — {v_done}/{len(valid_pairs)} checked "
                             f"({v_pass_live} passed, {v_done - v_pass_live} need retry)")

            _verify_sec = time.time() - _t_verify
            _v_pass = v_pass_live
            _v_fail = len(future_to_pair) - _v_pass
            write_timing_log(log_ctx, "verify",
                f"{len(valid_pairs)} q | {_verify_sec:.2f}s (parallel) | {_v_pass} pass, {_v_fail} fail")

            _verify_errs = [e for (_p, _r, errs) in verify_results.values() for e in errs]
            _critical_v = next((e for e in _verify_errs if _is_critical_error(e)), None)
            if _critical_v:
                _abort_run_critical(_critical_v)   # stops the run; st.rerun() raises

            for fut, (item, q) in future_to_pair.items():
                passed, reason, errors = verify_results[fut]
                for e in errors:
                    log_error(e)   # back on main thread — safe
                if passed:
                    continue       # already cached + counted live in the loop above
                elif reason.startswith("UNREACHABLE_URLS:"):
                    # All citation URLs were unfetchable — skip factual verification,
                    # re-queue with explicit feedback naming the bad URLs so the LLM
                    # knows to pick entirely different documentation sources.
                    bad_urls = reason[len("UNREACHABLE_URLS:"):].split("|")
                    url_list = "\n".join(f"  - {u}" for u in bad_urls if u)
                    item["attempts"] += 1
                    item["failure_type"] = "unreachable_url"
                    item["feedback"] = (
                        f"Your previous question's citation URLs could not be retrieved "
                        f"(the pages returned no content):\n{url_list}\n"
                        f"You MUST generate a COMPLETELY NEW question using DIFFERENT "
                        f"Snowflake documentation URLs. Do NOT reuse any of the URLs "
                        f"listed above. Choose a different section of the Snowflake docs "
                        f"that covers the same concept."
                    )
                    write_timing_log(log_ctx, "fail",
                        f"concept {item['concept_idx']} v{item['variation']} attempt {item['attempts']} "
                        f"type=unreachable_url :: {', '.join(u for u in bad_urls if u)[:160]}")
                    if q.get("question"):
                        item["failed_texts"].append(q["question"])
                    if item["attempts"] < MAX_GENERATION_ATTEMPTS:
                        still_pending.append(item)
                    else:
                        log_error(f"Concept {item['concept_idx']} (v{item['variation']}) exhausted all attempts: all citation URLs unreachable ({', '.join(bad_urls)})")
                else:
                    item["attempts"] += 1
                    item["failure_type"] = "factual"
                    item["feedback"] = reason
                    write_timing_log(log_ctx, "fail",
                        f"concept {item['concept_idx']} v{item['variation']} attempt {item['attempts']} "
                        f"type=factual :: {reason[:160]}")
                    if q.get("question"):
                        item["failed_texts"].append(q["question"])
                    if item["attempts"] < MAX_GENERATION_ATTEMPTS:
                        still_pending.append(item)
                    else:
                        log_error(f"Concept {item['concept_idx']} (v{item['variation']}) exhausted all attempts: {reason}")

        _t_save = time.time()
        save_questions_cache()
        _save_sec = time.time() - _t_save
        write_timing_log(log_ctx, "save", f"{_save_sec:.2f}s")
        write_timing_log(log_ctx, "total",
            f"{time.time() - batch_start:.2f}s | still_pending={len(still_pending)}")
        st.session_state.bulk_items = still_pending + remaining
        st.rerun()

    elif st.session_state.stop_bulk:
        cached_so_far = st.session_state.bulk_total - len(bulk_items)
        st.session_state.bulk_items = []
        st.session_state.bulk_total = 0
        st.session_state.stop_bulk  = False
        st.info(f"Generation stopped — {cached_so_far} questions cached so far.")

    elif not bulk_items and st.session_state.bulk_total > 0:
        # Completed naturally
        queued   = st.session_state.bulk_total
        cached   = len(st.session_state.generated_questions)
        st.session_state.bulk_total = 0
        errors   = queued - cached
        if errors > 0:
            st.warning(
                f"Done — {cached} of {queued} questions cached. "
                f"{errors} concept(s) exhausted all {MAX_GENERATION_ATTEMPTS} attempts without passing verification. "
                f"Press **Generate Missing Questions** to retry the missing ones."
            )
        else:
            st.success(f"Done — {cached} questions generated and cached.")


def render_home():
    st.markdown(get_snowflake_css(), unsafe_allow_html=True)
    st.markdown("""
    <div class="hero-header">
        <h1>❄️ SnowPro Core Exam Prep</h1>
        <p>Master the Snowflake COF-C03 Certification</p>
    </div>
    """, unsafe_allow_html=True)

    cols = st.columns(4)
    with cols[0]:
        st.markdown('<div class="stat-card"><h3>100</h3><p>Exam Questions</p></div>', unsafe_allow_html=True)
    with cols[1]:
        st.markdown('<div class="stat-card"><h3>115</h3><p>Minutes</p></div>', unsafe_allow_html=True)
    with cols[2]:
        st.markdown('<div class="stat-card"><h3>750</h3><p>Passing Score /1000</p></div>', unsafe_allow_html=True)
    with cols[3]:
        st.markdown(f'<div class="stat-card"><h3>{len(CONCEPT_BANK) * VARIATIONS_PER_CONCEPT}</h3><p>AI-Generated Questions</p></div>', unsafe_allow_html=True)

    st.write("")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("🎓 Practice Exam")
        st.write("Simulate the real exam: 100 questions, 115-minute timer, scored out of 1000.")
        if st.button("Start Practice Exam", type="primary", use_container_width=True, key="home_exam"):
            st.session_state.mode = "exam_setup"
            st.rerun()

        st.write("")
        st.subheader("🃏 Flashcards")
        st.write("Review key concepts with flip cards organized by domain.")
        if st.button("Study Flashcards", use_container_width=True, key="home_flash"):
            st.session_state.mode = "flashcards"
            st.rerun()

    with col2:
        st.subheader("📝 Study by Domain")
        st.write("Focus on specific domains. Get instant feedback and explanations.")
        if st.button("Choose a Domain", use_container_width=True, key="home_study"):
            st.session_state.mode = "study_select"
            st.rerun()

        st.write("")
        st.subheader("📈 Your Progress")
        st.write("Track your scores, identify weak areas, and monitor improvement.")
        if st.button("View Progress", use_container_width=True, key="home_progress"):
            st.session_state.mode = "progress"
            st.rerun()

    st.write("")
    st.subheader("ℹ️ Exam Domains")
    for d_id, d_info in DOMAINS.items():
        st.markdown(f"""
        <div class="domain-card">
            <strong>{d_info['icon']} Domain {d_id}: {d_info['name']}</strong>
            <span class="snowflake-badge" style="margin-left: 8px;">{d_info['weight']}</span>
        </div>
        """, unsafe_allow_html=True)


def generate_exam(num_questions=100):
    """Return a list of (concept_idx, variation) pairs to drive the exam,
    stratified by official COF-C03 domain weight so every exam mirrors the real
    blueprint (e.g. ~31% Architecture ... ~10% Data Collaboration).

    Within each domain the pairs are unique; a domain only repeats a pair if its
    weighted share demands more questions than it has unique (concept x variation)
    pairs available.
    """
    # Group all (concept_idx, variation) pairs by domain.
    domain_pools = {}
    for i, c in enumerate(CONCEPT_BANK):
        for v in range(VARIATIONS_PER_CONCEPT):
            domain_pools.setdefault(c["domain"], []).append((i, v))

    # Per-domain target counts from DOMAINS weights ("31%" -> 31), using
    # largest-remainder rounding so the counts sum exactly to num_questions.
    weights = {d: int(str(info["weight"]).rstrip("%")) for d, info in DOMAINS.items()}
    total_w = sum(weights.values()) or 1
    raw = {d: num_questions * w / total_w for d, w in weights.items()}
    counts = {d: int(raw[d]) for d in weights}
    leftover = num_questions - sum(counts.values())
    for d in sorted(weights, key=lambda d: raw[d] - int(raw[d]), reverse=True):
        if leftover <= 0:
            break
        counts[d] += 1
        leftover -= 1

    result = []
    for d, n in counts.items():
        pool = list(domain_pools.get(d, []))
        if not pool or n <= 0:
            continue
        random.shuffle(pool)
        picked = pool[:n]
        while len(picked) < n:            # weight needs more than unique pairs exist
            random.shuffle(pool)
            picked.extend(pool[:n - len(picked)])
        result.extend(picked)

    random.shuffle(result)               # interleave domains across the exam
    return result[:num_questions]


def _resolve_exam_question(item):
    """Resolve an exam identifier to its question dict.

    Items are (concept_idx, variation) pairs. Tolerates a bare int (legacy shape)
    so an exam already in progress before an upgrade still scores correctly.
    """
    if isinstance(item, (list, tuple)):
        concept_idx, variation = item
    else:
        concept_idx, variation = item, 0
    return get_or_generate_question(concept_idx, variation)


def render_exam_setup():
    st.markdown(get_snowflake_css(), unsafe_allow_html=True)
    st.markdown("""
    <div class="hero-header">
        <h1>❄️ Practice Exam</h1>
        <p>Simulate the real SnowPro Core certification experience</p>
    </div>
    """, unsafe_allow_html=True)

    col1, col2, col3 = st.columns(3)
    with col1:
        num_q = st.selectbox("Number of questions", [10, 25, 50, 100], index=3)
    with col2:
        timed = st.selectbox("Timer", ["Timed (115 min)", "Timed (55 min)", "No timer"])
    with col3:
        st.write("")
        st.write("")
        passing = "750 / 1000"
        st.write(f"Passing score: **{passing}**")

    st.info("The real exam has 100 multiple-choice and multiple-select questions in 115 minutes. Questions may have one or more correct answers.", icon="ℹ️")

    uncached_exam = sum(
        1 for i in range(len(CONCEPT_BANK))
        if (i, 0) not in st.session_state.generated_questions
    )
    if uncached_exam > 0:
        st.warning(
            f"{uncached_exam} of {len(CONCEPT_BANK)} exam questions are not yet cached — "
            "generation will interrupt the timer during the exam.",
            icon="⚠️"
        )
        if st.button("Pre-load questions first", key="goto_preload_from_exam"):
            st.session_state.mode = "preload"
            st.rerun()

    if st.button("Begin Exam", type="primary", use_container_width=True):
        st.session_state.exam_questions = generate_exam(num_q)
        st.session_state.exam_answers = {}
        st.session_state.exam_submitted = False
        st.session_state.exam_current_q = 0
        if "115" in timed:
            st.session_state.exam_start_time = time.time()
            st.session_state.exam_time_limit = 115 * 60
        elif "55" in timed:
            st.session_state.exam_start_time = time.time()
            st.session_state.exam_time_limit = 55 * 60
        else:
            st.session_state.exam_start_time = None
            st.session_state.exam_time_limit = None
        st.session_state.mode = "exam"
        st.rerun()


def render_exam():
    st.markdown(get_snowflake_css(), unsafe_allow_html=True)
    show_connection_warning_if_needed()
    questions = st.session_state.exam_questions
    total = len(questions)

    if st.session_state.exam_submitted:
        render_exam_results()
        return

    top_cols = st.columns([3, 1, 1])
    with top_cols[0]:
        answered = len(st.session_state.exam_answers)
        st.progress(answered / total, text=f"Question {st.session_state.exam_current_q + 1} of {total} ({answered} answered)")
    with top_cols[1]:
        if st.session_state.exam_start_time and st.session_state.exam_time_limit:
            elapsed = time.time() - st.session_state.exam_start_time
            remaining = max(0, st.session_state.exam_time_limit - elapsed)
            total_secs = int(remaining)
            color = "#dc3545" if remaining < 300 else "#29B5E8"
            components.html(f"""
            <div id="timer" style="font-size:2rem;font-weight:700;text-align:center;color:{color};font-family:Inter,sans-serif;padding:0.25rem 0;"></div>
            <script>
                var total = {total_secs};
                var timerEl = document.getElementById('timer');
                function fmt(t) {{
                    var m = Math.floor(t / 60), s = t % 60;
                    return '⏱️ ' + String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0');
                }}
                timerEl.textContent = fmt(total);
                var iv = setInterval(function() {{
                    total -= 1;
                    if (total < 0) total = 0;
                    timerEl.textContent = fmt(total);
                    if (total < 300) timerEl.style.color = '#dc3545';
                    if (total <= 0) {{
                        clearInterval(iv);
                        try {{
                            var btns = window.parent.document.querySelectorAll('button');
                            btns.forEach(function(b) {{
                                if (b.textContent.trim() === 'Submit Exam') b.click();
                            }});
                        }} catch(e) {{}}
                    }}
                }}, 1000);
            </script>
            """, height=50)
            if remaining <= 0:
                st.session_state.exam_submitted = True
                st.rerun()
    with top_cols[2]:
        if st.button("Submit Exam", type="primary"):
            st.session_state.exam_submitted = True
            record_exam_results()
            st.rerun()

    idx = st.session_state.exam_current_q
    exam_item = questions[idx]
    st.session_state.error_context = f"Exam | Q{idx + 1}/{len(questions)}"

    # Check pause BEFORE attempting generation — prevents re-running on every rerun
    if st.session_state.generation_paused:
        st.warning(
            "Question generation failed. See the **Errors** section below for details.",
            icon="⚠️"
        )
        col1, col2 = st.columns(2)
        with col1:
            if st.button("📤 Switch to Offline Mode", use_container_width=True, type="primary", key="exam_offline"):
                st.session_state.offline_mode = True
                st.session_state.generation_paused = False
                st.rerun()
        with col2:
            if st.button("🔄 Retry (continue online)", use_container_width=True, key="exam_retry"):
                st.session_state.generation_paused = False
                st.rerun()
        return

    q = _resolve_exam_question(exam_item)
    if q is None:
        st.error("Could not load question. Please refresh.")
        return

    st.markdown(f"""
    <div class="question-card">
        <span class="snowflake-badge">Domain {q['domain']} - {DOMAINS[q['domain']]['name']}</span>
        <h3 style="margin-top: 1rem;">Question {idx + 1}</h3>
        <p style="font-size: 1.1rem; line-height: 1.6;">{md_safe(q['question'])}</p>
    </div>
    """, unsafe_allow_html=True)

    q_key = f"exam_q_{q['id']}_{idx}"
    current_answer = st.session_state.exam_answers.get(idx, [])

    if q["type"] == "multi":
        st.caption("ℹ️ Select all that apply")
        selected = []
        for i, opt in enumerate(q["options"]):
            checked = i in current_answer
            if st.checkbox(md_safe(opt), value=checked, key=f"{q_key}_opt_{i}"):
                selected.append(i)
        if selected != current_answer:
            st.session_state.exam_answers[idx] = selected
    else:
        display_opts = [md_safe(o) for o in q["options"]]
        choice = st.radio(
            "Select your answer:",
            display_opts,
            index=current_answer[0] if current_answer else None,
            key=q_key,
            label_visibility="collapsed"
        )
        if choice is not None:
            selected_idx = display_opts.index(choice)
            st.session_state.exam_answers[idx] = [selected_idx]

    nav_cols = st.columns([1, 3, 1])
    with nav_cols[0]:
        if idx > 0:
            if st.button("⬅️ Previous", use_container_width=True):
                st.session_state.exam_current_q = idx - 1
                st.rerun()
    with nav_cols[2]:
        if idx < total - 1:
            if st.button("Next ➡️", use_container_width=True):
                st.session_state.exam_current_q = idx + 1
                st.rerun()

    st.write("")
    st.write("**Question Navigator**")
    nav_row_size = 10
    for row_start in range(0, total, nav_row_size):
        row_end = min(row_start + nav_row_size, total)
        cols = st.columns(nav_row_size)
        for j, col in enumerate(cols):
            q_num = row_start + j
            if q_num < total:
                with col:
                    is_answered = q_num in st.session_state.exam_answers
                    is_current = q_num == idx
                    label = f"{'**' if is_current else ''}{q_num + 1}{'**' if is_current else ''}"
                    btn_type = "primary" if is_current else ("secondary" if not is_answered else "secondary")
                    if st.button(
                        f"{'✓' if is_answered else ''}{q_num + 1}",
                        key=f"nav_{q_num}",
                        use_container_width=True,
                        type="primary" if is_current else "secondary"
                    ):
                        st.session_state.exam_current_q = q_num
                        st.rerun()


def record_exam_results():
    items = st.session_state.exam_questions
    answers = st.session_state.exam_answers
    resolved = [_resolve_exam_question(it) for it in items]
    correct = 0
    for idx, q in enumerate(resolved):
        if q is None:
            continue
        if sorted(answers.get(idx, [])) == sorted(q["answer"]):
            correct += 1

    total = len(items)
    score = int((correct / total) * 1000) if total > 0 else 0
    passed = score >= 750

    st.session_state.exam_history.append({
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "questions": total,
        "correct": correct,
        "score": score,
        "passed": passed,
    })

    for idx, q in enumerate(resolved):
        if q is None:
            continue
        is_correct = sorted(answers.get(idx, [])) == sorted(q["answer"])
        st.session_state.total_questions_answered += 1
        d = q["domain"]
        st.session_state.domain_stats_exam[d]["total"] += 1
        if is_correct:
            st.session_state.total_correct += 1
            st.session_state.domain_stats_exam[d]["correct"] += 1

    save_progress()


def render_exam_results():
    st.markdown(get_snowflake_css(), unsafe_allow_html=True)
    items = st.session_state.exam_questions
    answers = st.session_state.exam_answers
    resolved = [_resolve_exam_question(it) for it in items]
    total = len(items)
    correct = 0
    domain_results = {}

    for idx, q in enumerate(resolved):
        if q is None:
            continue
        user_answer = sorted(answers.get(idx, []))
        correct_answer = sorted(q["answer"])
        is_correct = user_answer == correct_answer
        if is_correct:
            correct += 1
        d = q["domain"]
        if d not in domain_results:
            domain_results[d] = {"correct": 0, "total": 0}
        domain_results[d]["total"] += 1
        if is_correct:
            domain_results[d]["correct"] += 1

    score = int((correct / total) * 1000) if total > 0 else 0
    passed = score >= 750
    pct = int((correct / total) * 100) if total > 0 else 0

    pass_text = "PASSED" if passed else "NOT YET PASSING"
    pass_color = "#28a745" if passed else "#dc3545"
    st.markdown(f"""
    <div class="score-display">
        <p style="font-size: 1.1rem; opacity: 0.8; margin-bottom: 0.5rem;">Your Score</p>
        <h2>{score} / 1000</h2>
        <p style="font-size: 1.2rem; color: {pass_color}; font-weight: 700; margin-top: 0.5rem;">{pass_text}</p>
        <p style="opacity: 0.7; margin-top: 0.3rem;">{correct} of {total} correct ({pct}%)</p>
    </div>
    """, unsafe_allow_html=True)

    st.write("")
    st.subheader("Domain Breakdown")
    for d_id in sorted(domain_results.keys()):
        d_info = DOMAINS[d_id]
        dr = domain_results[d_id]
        d_pct = int((dr["correct"] / dr["total"]) * 100) if dr["total"] > 0 else 0
        status = "✅ Strong" if d_pct >= 80 else ("⚠️ Review" if d_pct >= 60 else "❌ Weak")
        col1, col2 = st.columns([3, 1])
        with col1:
            st.write(f"**{d_info['icon']} Domain {d_id}: {d_info['name']}**")
            st.progress(d_pct / 100, text=f"{dr['correct']}/{dr['total']} ({d_pct}%)")
        with col2:
            st.write("")
            st.write(status)

    st.write("")
    st.subheader("Review All Questions")

    show_filter = st.radio("Filter", ["All", "Incorrect Only", "Correct Only"], index=0, key="results_filter", horizontal=True)

    for idx, q in enumerate(resolved):
        if q is None:
            continue
        user_answer = sorted(answers.get(idx, []))
        correct_answer = sorted(q["answer"])
        is_correct = user_answer == correct_answer

        if show_filter == "Incorrect Only" and is_correct:
            continue
        if show_filter == "Correct Only" and not is_correct:
            continue

        icon = "✅" if is_correct else "❌"
        color = "green" if is_correct else "red"

        with st.expander(f":{color}[{icon}] Q{idx + 1}: {md_safe(q['question'][:80])}..."):
            st.write(f"**{md_safe(q['question'])}**")
            for i, opt in enumerate(q["options"]):
                is_user = i in user_answer
                is_ans = i in correct_answer
                if is_ans and is_user:
                    st.markdown(f'<div class="correct-answer">✅ {md_safe(opt)}</div>', unsafe_allow_html=True)
                elif is_ans and not is_user:
                    st.markdown(f'<div class="correct-answer">✅ {md_safe(opt)} (correct answer)</div>', unsafe_allow_html=True)
                elif is_user and not is_ans:
                    st.markdown(f'<div class="wrong-answer">❌ {md_safe(opt)} (your answer)</div>', unsafe_allow_html=True)
                else:
                    st.write(f"&nbsp;&nbsp;&nbsp;&nbsp;{md_safe(opt)}")
            render_explanation_box(q["explanation"], q.get("citations", []))

    st.write("")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Take Another Exam", type="primary", use_container_width=True):
            st.session_state.mode = "exam_setup"
            st.rerun()
    with col2:
        if st.button("Back to Home", use_container_width=True):
            st.session_state.mode = "home"
            st.rerun()


def render_flashcards():
    st.markdown(get_snowflake_css(), unsafe_allow_html=True)
    st.markdown("""
    <div class="hero-header">
        <h1>❄️ Flashcards</h1>
        <p>Flip through key concepts for each domain</p>
    </div>
    """, unsafe_allow_html=True)

    domain_options = ["All Domains"] + [f"Domain {d}: {DOMAINS[d]['name']}" for d in DOMAINS]
    selected = st.selectbox("Select domain", domain_options, key="flash_domain_select")

    if selected == "All Domains":
        cards = FLASHCARDS
    else:
        d_id = selected.split(":")[0].replace("Domain ", "").strip()
        cards = [c for c in FLASHCARDS if c["domain"] == d_id]

    if not cards:
        st.warning("No flashcards for this domain yet.")
        return

    if st.session_state.flash_index >= len(cards):
        st.session_state.flash_index = 0

    card = cards[st.session_state.flash_index]

    st.write(f"**Card {st.session_state.flash_index + 1} of {len(cards)}**")
    st.progress((st.session_state.flash_index + 1) / len(cards))

    st.markdown(f'<span class="snowflake-badge">Domain {card["domain"]} - {DOMAINS[card["domain"]]["name"]}</span>', unsafe_allow_html=True)

    if not st.session_state.flash_flipped:
        st.markdown(f"""
        <div class="flashcard">
            <div class="flashcard-front">{md_safe(card['front'])}</div>
        </div>
        """, unsafe_allow_html=True)
    else:
        back_text = md_safe(card['back']).replace('\n', '<br>')
        st.markdown(f"""
        <div class="flashcard" style="border-color: #29B5E8;">
            <div class="flashcard-back">{back_text}</div>
        </div>
        """, unsafe_allow_html=True)

    st.write("")
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        if st.button("⬅️ Previous", use_container_width=True, disabled=st.session_state.flash_index == 0):
            st.session_state.flash_index -= 1
            st.session_state.flash_flipped = False
            st.rerun()
    with col2:
        flip_label = "🔄 Show Answer" if not st.session_state.flash_flipped else "🔄 Show Question"
        if st.button(flip_label, use_container_width=True, type="primary"):
            st.session_state.flash_flipped = not st.session_state.flash_flipped
            st.rerun()
    with col3:
        if st.button("Next ➡️", use_container_width=True, disabled=st.session_state.flash_index >= len(cards) - 1):
            st.session_state.flash_index += 1
            st.session_state.flash_flipped = False
            st.rerun()
    with col4:
        if st.button("🔀 Shuffle", use_container_width=True):
            random.shuffle(cards)
            st.session_state.flash_index = 0
            st.session_state.flash_flipped = False
            st.rerun()


def render_study_select():
    st.markdown(get_snowflake_css(), unsafe_allow_html=True)
    st.markdown("""
    <div class="hero-header">
        <h1>❄️ Study by Domain</h1>
        <p>Focus on specific areas to strengthen your knowledge</p>
    </div>
    """, unsafe_allow_html=True)

    for d_id, d_info in DOMAINS.items():
        domain_qs = [c for c in CONCEPT_BANK if c["domain"] == d_id]
        domain_total_qs = len(domain_qs) * VARIATIONS_PER_CONCEPT
        stats = st.session_state.domain_stats_study[d_id]
        pct = int((stats["correct"] / stats["total"]) * 100) if stats["total"] > 0 else 0
        domain_indices_set = {i for i, c in enumerate(CONCEPT_BANK) if c["domain"] == d_id}

        col1, col2, col3, col4, col5 = st.columns([3, 1, 1, 1, 1])
        with col1:
            st.markdown(f"""
            <div class="domain-card">
                <strong>{d_info['icon']} Domain {d_id}: {d_info['name']}</strong>
                <br><small>{d_info['weight']} of exam | {domain_total_qs} practice questions</small>
            </div>
            """, unsafe_allow_html=True)
        with col2:
            if stats["total"] > 0:
                st.write(f"📝 {pct}% ({stats['correct']}/{stats['total']})")
            else:
                st.write("Not started")
        with col3:
            if st.button("Study", key=f"study_d_{d_id}", use_container_width=True):
                st.session_state.study_domain = d_id
                st.session_state.study_q_index = 0
                st.session_state.study_show_answer = False
                st.session_state.mode = "study"
                st.rerun()
        with col4:
            if stats["total"] > 0:
                if st.button("↺ Stats", key=f"reset_study_d_{d_id}", use_container_width=True,
                             help="Reset scores for this domain (keeps cached questions)"):
                    st.session_state.domain_stats_study[d_id] = {"correct": 0, "total": 0}
                    st.session_state.total_questions_answered = max(0, st.session_state.total_questions_answered - stats["total"])
                    st.session_state.total_correct = max(0, st.session_state.total_correct - stats["correct"])
                    save_progress()
                    st.rerun()
        with col5:
            has_cached = any(k[0] in domain_indices_set for k in st.session_state.generated_questions)
            if has_cached:
                _gen_busy = _generation_in_progress()
                if st.button("↺ Qs", key=f"reset_qs_d_{d_id}", use_container_width=True,
                             disabled=_gen_busy,
                             help="Disabled while a pre-load run is in progress — stop it first."
                                  if _gen_busy else
                                  "Clear cached questions for this domain (forces fresh generation)"):
                    st.session_state.generated_questions = {
                        k: v for k, v in st.session_state.generated_questions.items()
                        if k[0] not in domain_indices_set
                    }
                    save_questions_cache()
                    st.rerun()


def render_study():
    st.markdown(get_snowflake_css(), unsafe_allow_html=True)
    show_connection_warning_if_needed()
    d_id = st.session_state.study_domain
    d_info = DOMAINS[d_id]

    # Build ordered list of concept indices for this domain
    domain_concept_indices = [
        i for i, c in enumerate(CONCEPT_BANK) if c["domain"] == d_id
    ]

    if not domain_concept_indices:
        st.warning("No concepts available for this domain yet.")
        if st.button("Back"):
            st.session_state.mode = "study_select"
            st.rerun()
        return

    n_concepts = len(domain_concept_indices)
    total_qs = n_concepts * VARIATIONS_PER_CONCEPT
    idx = st.session_state.study_q_index % total_qs
    concept_position = idx % n_concepts
    variation = idx // n_concepts
    concept_idx = domain_concept_indices[concept_position]
    st.session_state.error_context = f"Study | Domain {d_id} | Q{idx + 1}/{total_qs}"

    # Check pause BEFORE attempting generation — prevents re-running on every rerun
    if st.session_state.generation_paused:
        st.warning(
            "Question generation failed. See the **Errors** section below for details.",
            icon="⚠️"
        )
        col1, col2 = st.columns(2)
        with col1:
            if st.button("📤 Switch to Offline Mode", use_container_width=True, type="primary", key="study_offline"):
                st.session_state.offline_mode = True
                st.session_state.generation_paused = False
                st.rerun()
        with col2:
            if st.button("🔄 Retry (continue online)", use_container_width=True, key="study_retry"):
                # Evict any cached entry so a genuinely new question is generated
                cache_key_retry = (concept_idx, variation)
                st.session_state.generated_questions.pop(cache_key_retry, None)
                save_questions_cache()
                st.session_state.generation_paused = False
                st.rerun()
        return

    # If this question isn't cached yet, show a placeholder immediately so the
    # previous question's content doesn't persist while generation runs.
    cache_key = (concept_idx, variation)
    loading_msg = st.empty()
    if cache_key not in st.session_state.generated_questions:
        loading_msg.info("Generating question — please wait...")

    q = get_or_generate_question(concept_idx, variation=variation)
    loading_msg.empty()   # clear the placeholder whether it showed or not

    if q is None:
        log_error(f"Generation failed for concept {concept_idx} (v{variation}) after {MAX_GENERATION_ATTEMPTS} attempts")
        st.session_state.generation_paused = True
        st.rerun()
        return

    # Success — clear any retry counter for this concept
    st.session_state.gen_retries.pop(f"{concept_idx}:{variation}", None)

    st.write(f"**{d_info['icon']} Domain {d_id}: {d_info['name']}** — Question {idx + 1} of {total_qs}")
    st.progress((idx + 1) / total_qs)

    st.markdown(f"""
    <div class="question-card">
        <h3>Question {idx + 1}</h3>
        <p style="font-size: 1.1rem; line-height: 1.6;">{md_safe(q['question'])}</p>
    </div>
    """, unsafe_allow_html=True)

    q_key = f"study_{d_id}_{idx}_{concept_idx}"

    if not st.session_state.study_show_answer:
        if q["type"] == "multi":
            st.caption("ℹ️ Select all that apply")
            selected = []
            for i, opt in enumerate(q["options"]):
                if st.checkbox(md_safe(opt), key=f"{q_key}_opt_{i}"):
                    selected.append(i)

            if st.button("Check Answer", type="primary", use_container_width=True):
                if not selected:
                    st.warning("Please select at least one answer before checking.", icon="⚠️")
                else:
                    st.session_state.study_user_answer = sorted(selected)
                    st.session_state.study_show_answer = True
                    is_correct = sorted(selected) == sorted(q["answer"])
                    st.session_state.total_questions_answered += 1
                    st.session_state.domain_stats_study[d_id]["total"] += 1
                    if is_correct:
                        st.session_state.total_correct += 1
                        st.session_state.domain_stats_study[d_id]["correct"] += 1
                    save_progress()
                    st.rerun()
        else:
            display_opts = [md_safe(o) for o in q["options"]]
            choice = st.radio("Select your answer:", display_opts, index=None, key=q_key, label_visibility="collapsed")
            if st.button("Check Answer", type="primary", use_container_width=True):
                if choice is None:
                    st.warning("Please select an answer before checking.", icon="⚠️")
                else:
                    selected_idx = display_opts.index(choice)
                    st.session_state.study_user_answer = [selected_idx]
                    st.session_state.study_show_answer = True
                    is_correct = [selected_idx] == q["answer"]
                    st.session_state.total_questions_answered += 1
                    st.session_state.domain_stats_study[d_id]["total"] += 1
                    if is_correct:
                        st.session_state.total_correct += 1
                        st.session_state.domain_stats_study[d_id]["correct"] += 1
                    save_progress()
                    st.rerun()
    else:
        user_answer = st.session_state.get("study_user_answer", [])
        correct_answer = q["answer"]
        is_correct = sorted(user_answer) == sorted(correct_answer)

        if is_correct:
            st.success("Correct!", icon="✅")
        else:
            st.error("Incorrect", icon="❌")

        for i, opt in enumerate(q["options"]):
            is_user = i in user_answer
            is_ans = i in correct_answer
            if is_ans and is_user:
                st.markdown(f'<div class="correct-answer">✅ {md_safe(opt)}</div>', unsafe_allow_html=True)
            elif is_ans:
                st.markdown(f'<div class="correct-answer">✅ {md_safe(opt)} (correct answer)</div>', unsafe_allow_html=True)
            elif is_user:
                st.markdown(f'<div class="wrong-answer">❌ {md_safe(opt)} (your answer)</div>', unsafe_allow_html=True)
            else:
                st.write(f"&nbsp;&nbsp;&nbsp;&nbsp;{md_safe(opt)}")

        render_explanation_box(q["explanation"], q.get("citations", []))

        _render_challenge_box(cache_key, d_id, q, user_answer)

        st.write("")
        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("⬅️ Previous", use_container_width=True, disabled=idx == 0):
                st.session_state.study_q_index = idx - 1
                st.session_state.study_show_answer = False
                st.rerun()
        with col2:
            if st.button("Next Question ➡️", type="primary", use_container_width=True, disabled=idx >= total_qs - 1):
                st.session_state.study_q_index = idx + 1
                st.session_state.study_show_answer = False
                st.rerun()
        with col3:
            if st.button("⬅️ All Domains", use_container_width=True):
                st.session_state.mode = "study_select"
                st.rerun()


def _status_label(pct, total):
    if total == 0:
        return "⏳ Not started"
    if pct >= 80:
        return "✅ Strong"
    if pct >= 60:
        return "⚠️ Review"
    return "❗ Weak"


def render_progress():
    st.markdown(get_snowflake_css(), unsafe_allow_html=True)
    st.markdown("""
    <div class="hero-header">
        <h1>❄️ Your Progress</h1>
        <p>Track your preparation journey</p>
    </div>
    """, unsafe_allow_html=True)

    total = st.session_state.total_questions_answered
    correct = st.session_state.total_correct
    pct = int((correct / total) * 100) if total > 0 else 0

    cols = st.columns(4)
    with cols[0]:
        st.metric("Questions Answered", total)
    with cols[1]:
        st.metric("Correct Answers", correct)
    with cols[2]:
        st.metric("Overall Accuracy", f"{pct}%")
    with cols[3]:
        st.metric("Exams Taken", len(st.session_state.exam_history))

    # --- Domain Performance: side-by-side Study vs Exam ---
    st.write("")
    st.subheader("Domain Performance")
    st.caption("Study = questions answered in Study by Domain mode · Exam = questions answered in Practice Exam mode")

    hdr = st.columns([3, 2, 2])
    hdr[1].markdown("**📝 Study**")
    hdr[2].markdown("**🎓 Exam**")

    for d_id, d_info in DOMAINS.items():
        s = st.session_state.domain_stats_study[d_id]
        e = st.session_state.domain_stats_exam[d_id]
        s_pct = int((s["correct"] / s["total"]) * 100) if s["total"] > 0 else 0
        e_pct = int((e["correct"] / e["total"]) * 100) if e["total"] > 0 else 0

        col1, col2, col3 = st.columns([3, 2, 2])
        with col1:
            st.write(f"**{d_info['icon']} Domain {d_id}: {d_info['name']}**")
        with col2:
            if s["total"] > 0:
                st.progress(s_pct / 100, text=f"{s['correct']}/{s['total']} ({s_pct}%) {_status_label(s_pct, s['total'])}")
            else:
                st.write("⏳ Not started")
        with col3:
            if e["total"] > 0:
                st.progress(e_pct / 100, text=f"{e['correct']}/{e['total']} ({e_pct}%) {_status_label(e_pct, e['total'])}")
            else:
                st.write("⏳ Not started")

    # --- Exam History ---
    if st.session_state.exam_history:
        st.write("")
        st.subheader("Exam History")
        for i, exam in enumerate(reversed(st.session_state.exam_history)):
            status = "✅ PASSED" if exam["passed"] else "❌ NOT PASSING"
            st.write(f"**Exam {len(st.session_state.exam_history) - i}** — {exam['date']} — Score: **{exam['score']}/1000** — {exam['correct']}/{exam['questions']} correct — {status}")

    # --- Reset Controls ---
    st.write("")
    st.subheader("Reset Options")
    st.caption("Stats resets clear scores only. Question resets clear cached Q&A only. Both are permanent.")

    st.write("**Stats only** — clears scores, keeps cached questions")
    r1, r2 = st.columns(2)
    with r1:
        if st.button("↺ Reset Study Stats", use_container_width=True,
                     help="Clears study scores and accuracy. Cached questions are preserved."):
            study_correct = sum(v["correct"] for v in st.session_state.domain_stats_study.values())
            study_total = sum(v["total"] for v in st.session_state.domain_stats_study.values())
            st.session_state.domain_stats_study = {d: {"correct": 0, "total": 0} for d in DOMAINS}
            st.session_state.total_questions_answered = max(0, st.session_state.total_questions_answered - study_total)
            st.session_state.total_correct = max(0, st.session_state.total_correct - study_correct)
            save_progress()
            st.rerun()
    with r2:
        if st.button("↺ Reset Exam Stats", use_container_width=True,
                     help="Clears exam scores and history. Cached questions are preserved."):
            exam_correct = sum(v["correct"] for v in st.session_state.domain_stats_exam.values())
            exam_total = sum(v["total"] for v in st.session_state.domain_stats_exam.values())
            st.session_state.domain_stats_exam = {d: {"correct": 0, "total": 0} for d in DOMAINS}
            st.session_state.exam_history = []
            st.session_state.total_questions_answered = max(0, st.session_state.total_questions_answered - exam_total)
            st.session_state.total_correct = max(0, st.session_state.total_correct - exam_total)
            save_progress()
            st.rerun()

    st.write("**Questions only** — forces fresh generation, keeps scores")
    _gen_busy = _generation_in_progress()
    if _gen_busy:
        st.caption("⏳ A pre-load generation run is in progress — question resets are "
                   "disabled until it finishes or you stop it (on the Pre-load Questions page).")
    q1, _ = st.columns(2)
    with q1:
        if st.button("↺ Reset All Cached Questions", use_container_width=True, disabled=_gen_busy,
                     help="Disabled while a pre-load run is in progress — stop it first."
                          if _gen_busy else
                          "Clears all cached Q&A — every question will be regenerated fresh from Snowflake Cortex."):
            st.session_state.generated_questions = {}
            if os.path.exists(QUESTIONS_CACHE_FILE):
                os.remove(QUESTIONS_CACHE_FILE)
            st.rerun()

    st.divider()
    if st.button("↺ Reset Everything — clear all stats AND questions", type="secondary",
                 use_container_width=True, disabled=_gen_busy,
                 help="Disabled while a pre-load run is in progress — stop it first." if _gen_busy else None):
        st.session_state.total_questions_answered = 0
        st.session_state.total_correct = 0
        st.session_state.domain_stats_study = {d: {"correct": 0, "total": 0} for d in DOMAINS}
        st.session_state.domain_stats_exam = {d: {"correct": 0, "total": 0} for d in DOMAINS}
        st.session_state.exam_history = []
        st.session_state.generated_questions = {}
        if os.path.exists(PROGRESS_FILE):
            os.remove(PROGRESS_FILE)
        if os.path.exists(QUESTIONS_CACHE_FILE):
            os.remove(QUESTIONS_CACHE_FILE)
        st.rerun()


def render_reference():
    st.markdown(get_snowflake_css(), unsafe_allow_html=True)
    st.markdown("""
    <div class="hero-header">
        <h1>❄️ Quick Reference</h1>
        <p>Key facts and cheat sheets for the exam</p>
    </div>
    """, unsafe_allow_html=True)

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "☁️ Architecture & Features",
        "🛡️ Account Mgmt & Governance",
        "📤 Data Loading & Connectivity",
        "⚡ Performance & Querying",
        "🤝 Data Collaboration"
    ])

    with tab1:
        st.markdown("""
**Three-Layer Architecture**
| Layer | Purpose | Key Components |
|-------|---------|---------------|
| Cloud Services | Brain - coordination | Auth, query optimization, metadata, transactions |
| Compute | Muscle - execution | Virtual warehouses (independent clusters) |
| Storage | Foundation - data | Centralized, columnar micro-partitions |

**Micro-Partitions**: 50-500 MB uncompressed, immutable, columnar, auto-managed

**Warehouse Sizes & Credits/Hour**
| XS | S | M | L | XL | 2XL | 3XL | 4XL | 5XL | 6XL |
|----|---|---|---|----|-----|-----|-----|-----|-----|
| 1  | 2 | 4 | 8 | 16 | 32  | 64  | 128 | 256 | 512 |

**Warehouse Types**: Standard (general) vs Snowpark-Optimized (16x memory, for ML/Snowpark)

**Editions**: Standard < Enterprise < Business Critical < VPS
- Enterprise adds: multi-cluster WH, 90-day TT, masking, row access, auto-clustering
- Business Critical adds: Tri-Secret Secure, failover, HIPAA/PCI-DSS
- VPS adds: fully dedicated infrastructure

**Table Types**: Permanent, Temporary, Transient, Apache Iceberg, External, Dynamic

**View Types**: Standard, Materialized (Enterprise+), Secure

**Interfaces**: Snowsight (web UI), Snowflake CLI (snow), SnowSQL, IDE integrations (VS Code)

**AI/ML & App Development**
| Feature | What It Does |
|---------|-------------|
| Snowpark | Write Python/Java/Scala on Snowflake compute |
| Streamlit in Snowflake | Build interactive data apps natively |
| Snowflake Notebooks | Interactive SQL/Python/Markdown in Snowsight |
| Cortex AI Functions | COMPLETE, SUMMARIZE, CLASSIFY, EXTRACT, SENTIMENT, TRANSLATE |
| Cortex Search | Hybrid keyword+semantic search |
| Cortex Analyst | Natural language text-to-SQL |
| Snowflake ML | Forecasting, anomaly detection, model registry |

**Cloud Services Billing**: Free up to 10% of daily warehouse credits
        """, unsafe_allow_html=True)

    with tab2:
        st.markdown("""
**System Roles Hierarchy**
```
ACCOUNTADMIN (top - combines SYSADMIN + SECURITYADMIN)
├── SYSADMIN (warehouses, databases, all custom roles should roll up here)
└── SECURITYADMIN (grants, security)
    └── USERADMIN (users, roles)
PUBLIC (base - auto-granted to all)
ORGADMIN (organization-level, multi-account management)
```

**Access Control Models**
- **RBAC** (Role-Based): Privileges granted to roles, roles granted to users
- **DAC** (Discretionary): Owner of object controls access

**Authentication Methods**: Password, Key Pair (RSA), SAML SSO, OAuth, Federated
- **MFA Second Factors**: Passkeys (recommended), TOTP authenticator apps, Duo

**Network Policies**: IP allow/block lists at account or user level

**Data Governance Features**
| Feature | What It Does |
|---------|-------------|
| Dynamic Data Masking | Column-level, role-based, query-time (Enterprise+) |
| Row Access Policies | Filter rows based on role/attributes (Enterprise+) |
| Object Tagging | Tag objects for classification/governance |
| Privacy Policies | Aggregation constraints, differential privacy |
| Trust Center | Security recommendations, CIS benchmarks, risk assessments |
| Data Lineage | Track data flow through ACCESS_HISTORY |

**Monitoring & Cost Management**
| Tool | Purpose |
|------|---------|
| Resource Monitors | Control credit usage (notify/suspend at thresholds) |
| ACCOUNT_USAGE | 365-day history, account-wide, 45min-3hr latency |
| INFORMATION_SCHEMA | Real-time, 7-14 days, per-database |
| WAREHOUSE_LOAD_HISTORY | Warehouse utilization metrics |

**Encryption**: AES-256 at rest, TLS 1.2+ in transit (always on)
- Tri-Secret Secure: Customer-managed key + Snowflake key (Business Critical+)
        """, unsafe_allow_html=True)

    with tab3:
        st.markdown("""
**Loading Methods**
| Method | Type | Compute | Latency | Use Case |
|--------|------|---------|---------|----------|
| COPY INTO | Batch | Warehouse | Minutes | Bulk loading |
| Snowpipe | Continuous | Serverless | Seconds-min | Auto-ingest from stage |
| Snowpipe Streaming | Real-time | Serverless (SDK) | Sub-second | Row-level streaming |

**Stage Types**
| Type | Syntax | Notes |
|------|--------|-------|
| User | `@~` | One per user |
| Table | `@%table` | One per table |
| Named Internal | `@stage_name` | CREATE STAGE |
| Named External | `@stage_name` | Points to S3/Azure/GCS via Storage Integration |

**Automated Data Ingestion**
| Feature | Purpose |
|---------|---------|
| Streams | CDC - track INSERTs, UPDATEs, DELETEs on tables |
| Tasks | Scheduled SQL/stored procs (cron or interval) |
| Dynamic Tables | Declarative auto-refreshing tables (TARGET_LAG) |

**Connectors & Integrations**
| Type | Examples |
|------|---------|
| Drivers | JDBC, ODBC, Python, Node.js, Go, .NET |
| Connectors | Kafka, Spark, Python |
| Integrations | Storage, API, Git (for version-controlled code) |

**File Best Practices**: 100-250 MB compressed, split large files

**Supported Semi-Structured**: JSON, Avro, ORC, Parquet, XML → VARIANT type

**ON_ERROR Options**: ABORT_STATEMENT (default), CONTINUE, SKIP_FILE, SKIP_FILE_n

**Key Commands**: PUT (local→stage), GET (stage→local), COPY INTO, FLATTEN (nested→rows)
        """, unsafe_allow_html=True)

    with tab4:
        st.markdown("""
**Three Caches**
| Cache | Location | Duration | Benefit |
|-------|----------|----------|---------|
| Result Cache | Cloud Services | 24 hours | Free, instant results |
| Local Disk Cache | Warehouse SSD | While running | Fast micro-partition reads |
| Metadata Cache | Cloud Services | Always | Enables pruning |

**Scale UP vs OUT**
- **UP** (bigger size): Faster individual queries
- **OUT** (multi-cluster): More concurrent queries (Enterprise+)

**Multi-Cluster Scaling Policies**
- **Standard**: Add clusters immediately when queries queue
- **Economy**: Wait up to 6 minutes before adding clusters

**Performance Features**
| Feature | What It Does | Edition |
|---------|-------------|---------|
| Clustering Keys | Organize micro-partitions for pruning | Enterprise+ |
| Search Optimization | Speed up point lookups & substring searches | Enterprise+ |
| Query Acceleration (QAS) | Offload scans to serverless compute | Enterprise+ |
| Materialized Views | Precomputed, auto-maintained results | Enterprise+ |

**Query Profile / Query Insights**: Visual execution plan in Snowsight
- Check: bytes spilled (memory overflow), partition pruning, exploding joins, queuing

**Workload Best Practices**: Group similar workloads on same warehouse, isolate heavy ETL

**Data Transformation**
- Structured: standard SQL
- Semi-structured: dot/bracket notation on VARIANT, FLATTEN, LATERAL
- Unstructured: directory tables, pre-signed URLs, file functions
- Window functions: RANK, ROW_NUMBER, LAG, LEAD, NTILE
        """, unsafe_allow_html=True)

    with tab5:
        st.markdown("""
**Data Protection**
| Feature | Details |
|---------|---------|
| Time Travel | Query/restore historical data (0-90 days depending on edition & table type) |
| Fail-safe | 7 days after TT, Snowflake-support only (permanent tables only) |
| Cloning | Zero-copy, metadata-only, instant, copy-on-write |
| UNDROP | Restore dropped tables/schemas/databases within TT period |

**Time Travel by Table Type**
| Table Type | Max Retention | Fail-safe |
|------------|--------------|-----------|
| Permanent (Standard) | 1 day | 7 days |
| Permanent (Enterprise+) | 90 days | 7 days |
| Transient | 1 day | None |
| Temporary | 1 day (session only) | None |

**Replication**: Database/share replication available on ALL editions. **Failover/failback**: Business Critical+ only

**Secure Data Sharing**
- Live, read-only, no data copied, instant access
- Provider pays storage, Consumer pays compute
- Only secure views/UDFs can be shared
- Reader Accounts: For non-Snowflake users (provider pays compute)
- Direct Shares: Account-to-account
- Listings: Published to Marketplace (public or private)

**Data Clean Rooms**: Multi-party collaboration without exposing raw data

**Snowflake Marketplace**
- Public listings: Discoverable by all Snowflake users
- Private listings: Shared with specific accounts
- Native Apps: Full applications distributed via Marketplace

**Native Apps Framework**: Package and distribute applications (code + data + UI) to consumer accounts
        """, unsafe_allow_html=True)


def main():
    init_session_state()
    render_sidebar()

    mode = st.session_state.mode
    if mode == "home":
        render_home()
    elif mode == "exam_setup":
        render_exam_setup()
    elif mode == "exam":
        render_exam()
    elif mode == "flashcards":
        render_flashcards()
    elif mode == "study_select":
        render_study_select()
    elif mode == "study":
        render_study()
    elif mode == "progress":
        render_progress()
    elif mode == "reference":
        render_reference()
    elif mode == "preload":
        render_preload()

    render_error_log()


main()
