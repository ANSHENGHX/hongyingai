CREATE TABLE IF NOT EXISTS ai_render_run (
  run_id VARCHAR(96) PRIMARY KEY,
  task_id BIGINT NOT NULL,
  tenant_id BIGINT NOT NULL,
  run_no INT NOT NULL,
  stage VARCHAR(32) NOT NULL,
  progress DECIMAL(6,5) NOT NULL DEFAULT 0,
  sequence_no BIGINT NOT NULL DEFAULT 0,
  worker_id VARCHAR(255) NULL,
  lease_until DATETIME(6) NULL,
  attempt INT NOT NULL DEFAULT 0,
  output_object_key VARCHAR(1024) NULL,
  error_code VARCHAR(64) NULL,
  error_summary VARCHAR(1000) NULL,
  metadata_json JSON NOT NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  UNIQUE KEY uk_task_run_no (tenant_id, task_id, run_no),
  KEY idx_run_tenant (tenant_id, run_id),
  KEY idx_run_stage_updated (stage, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS ai_model_call_record (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  tenant_id BIGINT NOT NULL,
  task_id BIGINT NULL,
  trace_id VARCHAR(128) NOT NULL,
  provider VARCHAR(64) NOT NULL,
  model VARCHAR(128) NOT NULL,
  prompt_id VARCHAR(128) NOT NULL,
  prompt_version VARCHAR(64) NOT NULL,
  prompt_hash CHAR(64) NOT NULL,
  input_tokens INT NULL,
  output_tokens INT NULL,
  latency_ms INT NOT NULL,
  cost_usd DECIMAL(12,6) NULL,
  safety_status VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN',
  fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
  schema_failure BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  KEY idx_model_task (tenant_id, task_id, created_at),
  KEY idx_model_prompt (prompt_id, prompt_version, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS ai_cost_record (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  tenant_id BIGINT NOT NULL,
  task_id BIGINT NOT NULL,
  run_id VARCHAR(96) NULL,
  model_tokens BIGINT NOT NULL DEFAULT 0,
  gpu_seconds DECIMAL(12,3) NOT NULL DEFAULT 0,
  cpu_seconds DECIMAL(12,3) NOT NULL DEFAULT 0,
  input_media_seconds DECIMAL(12,3) NOT NULL DEFAULT 0,
  output_media_seconds DECIMAL(12,3) NOT NULL DEFAULT 0,
  storage_bytes BIGINT NOT NULL DEFAULT 0,
  transfer_bytes BIGINT NOT NULL DEFAULT 0,
  estimated_cost_usd DECIMAL(12,6) NOT NULL DEFAULT 0,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  KEY idx_cost_task (tenant_id, task_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS ai_prompt_registry (
  prompt_id VARCHAR(128) NOT NULL,
  version VARCHAR(64) NOT NULL,
  owner VARCHAR(128) NOT NULL,
  input_schema JSON NOT NULL,
  output_schema JSON NOT NULL,
  prompt_hash CHAR(64) NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (prompt_id, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS ai_brand_knowledge (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  tenant_id BIGINT NOT NULL,
  source_id VARCHAR(128) NOT NULL,
  source_type VARCHAR(64) NOT NULL,
  title VARCHAR(255) NOT NULL,
  content TEXT NOT NULL,
  metadata_json JSON NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),
  UNIQUE KEY uk_knowledge_source (tenant_id, source_id),
  FULLTEXT KEY ft_knowledge (title, content),
  KEY idx_knowledge_tenant (tenant_id, enabled, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
