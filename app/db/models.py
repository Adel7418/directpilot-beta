from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    MetaData,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.modules.tenancy.models import (
    MembershipRole,
    MembershipStatus,
    UserStatus,
    WorkspaceKind,
    WorkspaceStatus,
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UserRecord(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    yandex_subject: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    email_normalized: Mapped[str | None] = mapped_column(String(320), nullable=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        SqlEnum(
            UserStatus,
            native_enum=False,
            values_callable=lambda values: [value.value for value in values],
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspaceRecord(Base):
    __tablename__ = "workspaces"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[WorkspaceKind] = mapped_column(
        SqlEnum(
            WorkspaceKind,
            native_enum=False,
            values_callable=lambda values: [value.value for value in values],
        ),
        nullable=False,
    )
    status: Mapped[WorkspaceStatus] = mapped_column(
        SqlEnum(
            WorkspaceStatus,
            native_enum=False,
            values_callable=lambda values: [value.value for value in values],
        ),
        nullable=False,
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MembershipRecord(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    role: Mapped[MembershipRole] = mapped_column(
        SqlEnum(
            MembershipRole,
            native_enum=False,
            values_callable=lambda values: [value.value for value in values],
        ),
        nullable=False,
    )
    status: Mapped[MembershipStatus] = mapped_column(
        SqlEnum(
            MembershipStatus,
            native_enum=False,
            values_callable=lambda values: [value.value for value in values],
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SessionRecord(Base):
    __tablename__ = "sessions"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    active_workspace_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OAuthTransactionRecord(Base):
    __tablename__ = "yandex_oauth_transactions"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id"),
        nullable=False,
    )
    browser_session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("sessions.id"),
        nullable=False,
    )
    return_path: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExternalIdentityRecord(Base):
    __tablename__ = "external_identities"
    __table_args__ = (
        UniqueConstraint("issuer", "subject"),
        UniqueConstraint("user_id", "issuer"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    profile_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    profile_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_authenticated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class YandexProviderConnectionRecord(Base):
    __tablename__ = "yandex_provider_connections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "provider", "external_identity_id"),
        UniqueConstraint("id", "workspace_id"),
        CheckConstraint("provider = 'yandex'", name="provider_yandex"),
        CheckConstraint("schema_version > 0", name="schema_version_positive"),
        CheckConstraint("version > 0", name="version_positive"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id"),
        nullable=False,
    )
    external_identity_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("external_identities.id"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    token_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    token_nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrap_nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    kek_key_id: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    access_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    credential_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class ProviderAccountRecord(Base):
    __tablename__ = "provider_accounts"
    __table_args__ = (
        UniqueConstraint("connection_id", "provider_account_key"),
        ForeignKeyConstraint(
            ["connection_id", "workspace_id"],
            ["yandex_provider_connections.id", "yandex_provider_connections.workspace_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "account_type IN ('advertiser', 'agency_client')",
            name="account_type_valid",
        ),
        CheckConstraint(
            "status IN ('active', 'archived', 'stale')",
            name="status_valid",
        ),
        CheckConstraint(
            "capabilities = '[\"direct.read\"]'::jsonb",
            name="capabilities_read_only",
        ),
        CheckConstraint("login_schema_version > 0", name="login_schema_version_positive"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("octet_length(login_nonce) = 12", name="login_nonce_length"),
        CheckConstraint("octet_length(login_wrap_nonce) = 12", name="login_wrap_nonce_length"),
        CheckConstraint("char_length(login_kek_key_id) > 0", name="login_kek_key_id_not_empty"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id"),
        nullable=False,
        index=True,
    )
    connection_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    provider_account_key: Mapped[str] = mapped_column(String(255), nullable=False)
    account_type: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    country_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    login_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    login_nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    login_wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    login_wrap_nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    login_kek_key_id: Mapped[str] = mapped_column(String(128), nullable=False)
    login_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    last_discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class AuditEventRecord(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id"),
        nullable=True,
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity: Mapped[str] = mapped_column(String(255), nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("workspace_id", "namespace", "key_hash"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id"),
        nullable=False,
    )
    namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CampaignDraftRecord(Base):
    __tablename__ = "campaign_drafts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id"),
        primary_key=True,
        nullable=False,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class SemanticChangePackageRecord(Base):
    __tablename__ = "semantic_change_packages"

    package_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id"),
        primary_key=True,
        nullable=False,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
