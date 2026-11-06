import os, shutil, uuid
from pathlib import Path
from functools import cached_property
from runtime.artifacts import read, save
from runtime.registry import folder, INTERNAL
from shared.storage import read_csv


class Context:
    def __init__(self, root, run, number, config, controller):
        self.root = Path(root)
        self.run = Path(run)
        self.number = number
        self.config = config
        self.mode = config["mode"]
        self.controller = controller
        self.work = self.run / "work" / folder(number)
        self.work.mkdir(parents=True, exist_ok=True)
        self.pending = self.work / "publish"
        if self.pending.exists():
            self.pending.rename(self.work / ("uncommitted_publish_" + uuid.uuid4().hex))
        self.pending.mkdir()
        self.communications = self.run / "communications" / folder(number)
        self.communications.mkdir(parents=True, exist_ok=True)
        self._transport = None
        self.sourcepaths = {
            "chunks": self.upstream(1, "chunks.csv"),
            "documents": self.upstream(1, "documents.csv"),
            "summaries": self.upstream(8, "document_summaries.csv"),
            "abstracts": self.upstream(1, "document_abstracts.csv"),
            "references": self.upstream(9, "reference_inventory.csv"),
        }
        os.environ["ENTITY_EXECUTION_ID"] = config["identity"]
        if number in {4, 5, 6, 7, 8, 9, 10, 12}:
            self.bind_entity()

    @cached_property
    def documents_source(self):
        from runtime.agent_contract import document_folder

        return document_folder(self.root, self.run, self.number)

    def source(self, relative):
        path = Path(relative)
        if path.parts and path.parts[0] == "02_documents":
            return self.documents_source.joinpath(*path.parts[1:])
        return self.root / "inputs" / relative

    def upstream(self, number, name):
        return self.run / "outputs" / folder(number) / name

    def publish(self, path, name=None):
        path = Path(path)
        dest = self.pending / (name or path.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        return dest

    def emit(self, name, value):
        save(self.pending / name, value)

    def status(self, **values):
        save(self.run / "state" / folder(self.number) / "progress.json", values)

    def bind_entity(self):
        from shared.selected_entity import SelectedEntity, bind_entity

        path = self.upstream(3, "entity_selection.json")
        bind_entity(SelectedEntity.from_selection(read(path), str(path)))

    @cached_property
    def cfg(self):
        from shared.settings import PipelineConfig

        cfg = PipelineConfig(
            self.work / ("claims_runtime" if self.number == 4 else "lib2_" + uuid.uuid4().hex),
            Path("."),
            Path("."),
            claim_discovery_batch_chunks=int(
                self.config.get("extraction_batch_chunks", 4)
            ),
            claim_discovery_batch_chars=int(
                self.config.get("extraction_batch_chars", 12000)
            ),
            claim_discovery_chunk_workers=self.config["max_concurrency"],
            claim_discovery_doc_workers=self.config["max_concurrency"],
            claim_discovery_provider_max_attempts=1,
            step3b_llm_verifier_workers=self.config["max_concurrency"],
            step3b_llm_verifier_max_attempts=1,
        )
        for key, dst in [
            ("chunks", cfg.chunk_manifest_path),
            ("documents", cfg.doc_text_index_path),
        ]:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copy2(self.sourcepaths[key], dst)
        cfg.claim_generation_dir.mkdir(parents=True, exist_ok=True)
        return cfg

    @cached_property
    def chunks(self):
        return read_csv(self.sourcepaths["chunks"])

    @cached_property
    def docs(self):
        rows = read_csv(self.sourcepaths["documents"])
        for row in rows:
            if row.get("fulltext_path"):
                path = Path(row["fulltext_path"])
                if not path.is_absolute():
                    row["fulltext_path"] = str(
                        (self.sourcepaths["documents"].parent / path).resolve()
                    )
        if self.number == 4:
            from shared.text_cleaning import clean_text_for_model

            for row in rows:
                path = Path(row["fulltext_path"]) if row.get("fulltext_path") else None
                row["doc_text"] = (
                    path.read_text(encoding="utf-8")
                    if path and path.is_file()
                    else str(row.get("doc_text", "") or row.get("fulltext", "") or "")
                )
                row["doc_clean_text"] = clean_text_for_model(row["doc_text"])
        return rows

    @cached_property
    def context(self):
        from claims import clustering as c

        return c.Step3RunContext(
            self.cfg,
            [],
            self.chunks,
            self.docs,
            c._build_doc_type_map(self.chunks, self.docs),
            c._resolve_total_doc_count(self.docs, self.chunks),
        )

    @cached_property
    def raw(self):
        return read_csv(self.upstream(4, "01_raw_claim_candidates.csv"))

    @cached_property
    def structured(self):
        return read_csv(self.upstream(5, "02_structured_claim_pool.csv"))

    @cached_property
    def themes(self):
        return read_csv(self.upstream(7, "04_consensus_claims.csv"))

    @cached_property
    def scoring_raw(self):
        from runtime.scoring_scope import select_scoring_claims
        return select_scoring_claims(self.raw, self.themes)

    def hydrate(self, number):
        for path in (self.run / "outputs" / folder(number)).glob("*.csv"):
            target = (
                self.cfg.support_evidence_rows_path
                if path.name == "01_support_evidence_rows.csv"
                else self.cfg.claim_generation_dir / path.name
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

    def setup_models(self):
        from claims import clustering as c, model_tasks as mt, abstraction as ab
        from claims.checkpoints import ValidatedCheckpoints
        from scoring import model_response
        from runtime.transport import make_transport

        transport, keys = make_transport(self)
        self._transport = transport
        mt._stage5_checkpoints = (
            ValidatedCheckpoints(self.work / 'validated_responses') if self.number == 5 else None
        )
        self._checkpoints = mt._stage5_checkpoints
        transport.set_stage(INTERNAL[self.number])
        from functools import partial
        from runtime.concurrency import AdaptiveExecutor
        from claims import extraction

        factory = partial(
            AdaptiveExecutor, transport=transport, controller=self.controller
        )
        c.ThreadPoolExecutor = factory
        extraction.ThreadPoolExecutor = factory
        for module in (c, mt, ab, model_response):
            if hasattr(module, "_request_responses_api"):
                module._request_responses_api = transport.request
        mt._request_responses_api_async = transport.request_async

        async def judge_async(*args, **kwargs):
            return await transport.call_async(
                mt.run_llm_cluster_candidate_judge, *args, **kwargs
            )

        mt.run_llm_cluster_candidate_judge_async = judge_async
        c.run_llm_cluster_candidate_judge_async = judge_async
        from runtime.stage_parameters import resolve_model

        default_model = "gpt-5.4-mini" if self.number in (4, 5) else "gpt-5.4"
        stage_model = resolve_model(self.run, self.number, self.config, default_model)
        if self.number == 5:
            from datetime import datetime, timezone

            save(
                self.communications / "model_selections" / (uuid.uuid4().hex + ".json"),
                {
                    "stage": 5,
                    "model": stage_model,
                    "default_model": default_model,
                    "selection_source": "approval_snapshot"
                    if "_approved_stage_model" in self.config
                    else "run_parameters_or_default",
                    "time_utc": datetime.now(timezone.utc).isoformat(),
                    "pid": os.getpid(),
                },
            )
        os.environ.update(
            PIPELINE_STEP3_ASYNC="1",
            LLM_API_KEY=keys[0],
            LLM_API_BASE_URL=transport.base,
            LLM_MODEL_NAME="gpt-5.4-mini",
            STEP3B_LLM_MODEL_NAME=stage_model,
        )
        env = {
            "LLM_API_KEY": keys[0],
            "LLM_API_BASE_URL": transport.base,
            "LLM_MODEL_NAME": stage_model,
        }
        c.require_llm_env = lambda *args, **kwargs: dict(env)
        return (transport, env)

    def close(self):
        if self._transport is not None:
            self._transport.close()
            from claims import model_tasks
            if getattr(self, '_checkpoints', None) is model_tasks._stage5_checkpoints:
                model_tasks._stage5_checkpoints = None
