ALTER TABLE research_wave_results
ADD COLUMN execution_completed_run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT;

ALTER TABLE research_wave_results
ADD COLUMN execution_completed_at TEXT;

ALTER TABLE research_wave_results
ADD COLUMN checkpointed_run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT;

UPDATE research_wave_results
SET execution_completed_run_id = run_id
WHERE phase = 'execution_completed';

UPDATE research_wave_results
SET execution_completed_at = recorded_at
WHERE phase = 'execution_completed';

UPDATE research_wave_results
SET checkpointed_run_id = run_id
WHERE checkpointed_at IS NOT NULL;

CREATE INDEX research_wave_results_completion_run_idx
ON research_wave_results (execution_completed_run_id, checkpointed_run_id, wave_number);
