from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import CampaignDraftRecord, SemanticChangePackageRecord
from app.models import (
    Campaign,
    CampaignDraft,
    CampaignDraftRequest,
    SemanticChangePackage,
    YandexControlResult,
)
from app.modules.actions.idempotency import (
    IdempotencyClaim,
    IdempotencyStateError,
    PostgresIdempotencyRepository,
)
from app.modules.audit.repository import PostgresAuditRepository
from app.store import MockStore


class PostgresLegacyStoreRepository:
    """P2 persistence adapter: PostgreSQL owns drafts, packages, and audit records.

    The legacy engine is retained only to preserve existing draft/package behavior;
    its process-local dictionaries are refreshed from and written back to PostgreSQL
    for every persistent P2 operation.
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions
        self._audit = PostgresAuditRepository(sessions)
        self._idempotency = PostgresIdempotencyRepository(sessions)
        self._legacy = MockStore()
        self._legacy.append_audit = self.append_audit

    @property
    def campaigns(self) -> Mapping[str, Campaign]:
        return {
            draft.id: Campaign(
                id=draft.id,
                name=draft.name or f"Draft: {draft.business_type} / {draft.region}",
                business_type=draft.business_type,
                status=draft.status,
            )
            for draft in self.drafts.values()
        }

    @property
    def drafts(self) -> Mapping[str, CampaignDraft]:
        self._hydrate_drafts()
        return self._legacy.drafts

    @property
    def recommendations(self) -> Mapping[str, Any]:
        return MappingProxyType({})

    @property
    def audit_events(self) -> list[Any]:
        return self._audit.audit_events

    def append_audit(self, *args: Any, **kwargs: Any) -> Any:
        return self._audit.append_audit(*args, **kwargs)

    def create_draft(self, payload: CampaignDraftRequest) -> CampaignDraft:
        self._hydrate_drafts()
        draft = self._legacy.create_draft(payload)
        self._save_draft(draft)
        return draft

    def update_draft_base(self, draft_id: str, updates: dict[str, Any]) -> CampaignDraft:
        return self._mutate_draft("update_draft_base", draft_id, updates)

    def replace_keywords(self, draft_id: str, keywords: list[str]) -> CampaignDraft:
        return self._mutate_draft("replace_keywords", draft_id, keywords)

    def append_keywords(self, draft_id: str, keywords: list[str]) -> CampaignDraft:
        return self._mutate_draft("append_keywords", draft_id, keywords)

    def remove_keywords(self, draft_id: str, keywords: list[str]) -> CampaignDraft:
        return self._mutate_draft("remove_keywords", draft_id, keywords)

    def replace_negative_keywords(self, draft_id: str, payload: Any) -> CampaignDraft:
        return self._mutate_draft("replace_negative_keywords", draft_id, payload)

    def create_ad_group(self, draft_id: str, payload: Any) -> CampaignDraft:
        return self._mutate_draft("create_ad_group", draft_id, payload)

    def update_ad_group(self, draft_id: str, group_id: str, payload: Any) -> CampaignDraft:
        return self._mutate_draft("update_ad_group", draft_id, group_id, payload)

    def delete_ad_group(self, draft_id: str, group_id: str) -> CampaignDraft:
        return self._mutate_draft("delete_ad_group", draft_id, group_id)

    def create_ad(self, draft_id: str, payload: Any) -> CampaignDraft:
        return self._mutate_draft("create_ad", draft_id, payload)

    def update_ad(self, draft_id: str, ad_id: str, payload: Any) -> CampaignDraft:
        return self._mutate_draft("update_ad", draft_id, ad_id, payload)

    def delete_ad(self, draft_id: str, ad_id: str) -> CampaignDraft:
        return self._mutate_draft("delete_ad", draft_id, ad_id)

    def generate_structure(self, draft_id: str, payload: Any) -> CampaignDraft:
        return self._mutate_draft("generate_structure", draft_id, payload)

    def update_budget(self, draft_id: str, payload: Any) -> CampaignDraft:
        return self._mutate_draft("update_budget", draft_id, payload)

    def update_bids(self, draft_id: str, payload: Any) -> CampaignDraft:
        return self._mutate_draft("update_bids", draft_id, payload)

    def validate_draft(self, draft_id: str) -> Any:
        self._hydrate_drafts()
        return self._legacy.validate_draft(draft_id)

    def preview_draft(self, draft_id: str) -> Any:
        self._hydrate_drafts()
        return self._legacy.preview_draft(draft_id)

    def save_semantic_package(self, package: SemanticChangePackage) -> None:
        payload = package.model_dump(mode="json")
        with self._sessions() as session:
            with session.begin():
                record = session.get(SemanticChangePackageRecord, package.package_id)
                if record is None:
                    session.add(
                        SemanticChangePackageRecord(package_id=package.package_id, payload=payload)
                    )
                else:
                    record.payload = payload
                    record.updated_at = datetime.now(timezone.utc)

    def get_semantic_package(self, package_id: str) -> SemanticChangePackage:
        with self._sessions() as session:
            record = session.get(SemanticChangePackageRecord, package_id)
        if record is None:
            raise KeyError("semantic_change_package_not_found")
        return SemanticChangePackage.model_validate(record.payload)

    def prepare_semantic_change_package(self, *args: Any, **kwargs: Any) -> SemanticChangePackage:
        self._hydrate_packages()
        package = self._legacy.prepare_semantic_change_package(*args, **kwargs)
        self.save_semantic_package(package)
        return package

    def apply_semantic_change(self, *args: Any, **kwargs: Any) -> Any:
        self._hydrate_packages()
        result = self._legacy.apply_semantic_change(*args, **kwargs)
        for package in self._legacy.semantic_packages_by_id.values():
            self.save_semantic_package(package)
        return result

    def _mutate_draft(self, method: str, *args: Any) -> CampaignDraft:
        self._hydrate_drafts()
        handler: Callable[..., CampaignDraft] = getattr(self._legacy, method)
        draft = handler(*args)
        self._save_draft(draft)
        return draft

    def _hydrate_drafts(self) -> None:
        with self._sessions() as session:
            records = session.scalars(select(CampaignDraftRecord)).all()
        self._legacy.drafts = {
            record.id: CampaignDraft.model_validate(record.payload) for record in records
        }

    def _save_draft(self, draft: CampaignDraft) -> None:
        payload = draft.model_dump(mode="json")
        with self._sessions() as session:
            with session.begin():
                record = session.get(CampaignDraftRecord, draft.id)
                if record is None:
                    session.add(CampaignDraftRecord(id=draft.id, payload=payload))
                else:
                    record.payload = payload
                    record.updated_at = datetime.now(timezone.utc)

    def _hydrate_packages(self) -> None:
        with self._sessions() as session:
            records = session.scalars(select(SemanticChangePackageRecord)).all()
        self._legacy.semantic_packages_by_id = {
            record.package_id: SemanticChangePackage.model_validate(record.payload)
            for record in records
        }

    def live_create_campaign(self, *args: Any, **kwargs: Any) -> Any:
        self._hydrate_drafts()
        return self._legacy.live_create_campaign(*args, **kwargs)

    @staticmethod
    def _idempotency_namespace(operation: str, subject: str) -> str:
        subject_hash = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:32]
        return f"{operation}:{subject_hash}"

    @staticmethod
    def _replay_control(claim: IdempotencyClaim) -> YandexControlResult:
        if claim.status == "succeeded" and isinstance(claim.result, dict):
            return YandexControlResult.model_validate(claim.result)
        raise IdempotencyStateError("idempotency request is not replayable")

    def yandex_control(
        self,
        campaign_id: str,
        action: Literal["pause", "resume"],
        payload: Any,
        **kwargs: Any,
    ) -> YandexControlResult:
        claim = self._idempotency.claim(
            self._idempotency_namespace("yandex_control", f"{action}:{campaign_id}"),
            payload.idempotency_key,
            {
                "action": action,
                "campaign_id": campaign_id,
                "payload": payload.model_dump(mode="json"),
            },
        )
        if not claim.is_owner:
            return self._replay_control(claim)

        try:
            result = self._legacy.yandex_control(campaign_id, action, payload, **kwargs)
        except Exception as error:
            self._idempotency.complete(
                claim,
                status="failed",
                result={"error_type": type(error).__name__},
            )
            raise

        completed = self._idempotency.complete(
            claim,
            status="succeeded",
            result=result.model_dump(mode="json"),
        )
        return self._replay_control(completed)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._legacy, name)
