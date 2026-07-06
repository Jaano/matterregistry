"""baseline schema v0.3.12

Squashed baseline that collapses the original migration chain (0001-0028) into
a single revision. The statements below are the exact schema of a database that
migrated up through the full chain, captured verbatim from the production DB, so
a fresh install is byte-identical to an incrementally-migrated one.

Existing databases are already stamped ``0028`` and treat this revision as a
no-op; only fresh installs run the CREATE statements.

Revision ID: 0028
Revises:
Create Date: 2026-07-06

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Exact DDL captured verbatim from a fully-migrated production database
# (revision 0028). Stored as repr()-encoded literals for byte-perfect fidelity.
_CREATE_STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE \"property\" (\n\tid TEXT NOT NULL, \n\tdevice_id TEXT NOT NULL, \n\ttype TEXT NOT NULL, \n\tvalue TEXT NOT NULL, \n\tlabel TEXT, \n\tsource TEXT DEFAULT 'manual' NOT NULL, \n\tcaptured_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(device_id) REFERENCES device (id) ON DELETE CASCADE\n)",
    "CREATE TABLE attachment (\n\tid TEXT NOT NULL, \n\tdevice_id TEXT NOT NULL, \n\tkind TEXT NOT NULL, \n\tfilename TEXT NOT NULL, \n\tmime_type TEXT NOT NULL, \n\tsha256 TEXT NOT NULL, \n\tsize_bytes INTEGER NOT NULL, \n\tcontent BLOB NOT NULL, \n\tuploaded_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(device_id) REFERENCES device (id) ON DELETE CASCADE\n)",
    "CREATE TABLE matter_node_record (\n\tnode_id INTEGER NOT NULL, \n\tavailable BOOLEAN DEFAULT '0' NOT NULL, \n\tvendor_id INTEGER, \n\tvendor_name TEXT, \n\tproduct_id INTEGER, \n\tproduct_name TEXT, \n\tserial TEXT, \n\thardware_version_string TEXT, \n\tfirmware_version_string TEXT, \n\tnode_label TEXT, \n\tunique_id TEXT, \n\tmanufacturing_date TEXT, \n\tproduct_url TEXT, \n\tpart_number TEXT, \n\tnetwork_type_json TEXT DEFAULT '[]' NOT NULL, \n\tmac_address TEXT, \n\tendpoint_json TEXT DEFAULT '{}' NOT NULL, \n\tlast_synced DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, ip_addresses_json VARCHAR DEFAULT '[]' NOT NULL, date_commissioned VARCHAR, is_bridge BOOLEAN DEFAULT '0' NOT NULL, product_label VARCHAR, product_appearance_json VARCHAR, spec_version_int INTEGER, hardware_version_int INTEGER, software_version_int INTEGER, \n\tPRIMARY KEY (node_id)\n)",
    "CREATE TABLE device_link (\n\tid VARCHAR NOT NULL, \n\tdevice_id VARCHAR NOT NULL, \n\tintegration VARCHAR NOT NULL, \n\texternal_id VARCHAR NOT NULL, \n\tlink_source VARCHAR DEFAULT 'auto' NOT NULL, \n\tlinked_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_device_link_device_integration UNIQUE (device_id, integration), \n\tFOREIGN KEY(device_id) REFERENCES device (id)\n)",
    "CREATE INDEX ix_device_link_device_id ON device_link (device_id)",
    'CREATE TABLE "fabric" (\n\tid INTEGER NOT NULL, \n\tfabric_label TEXT, \n\tfabric_id TEXT NOT NULL, \n\tcontroller TEXT NOT NULL, \n\troot_ca_fingerprint TEXT, \n\tnotes TEXT, vendor_id INTEGER, vendor_name VARCHAR, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_fabric_fabric_id UNIQUE (fabric_id)\n)',
    "CREATE TABLE \"device_fabric_membership\" (\n\tid INTEGER NOT NULL, \n\tdevice_id TEXT NOT NULL, \n\tfabric_id INTEGER NOT NULL, \n\tnode_id INTEGER NOT NULL, \n\tendpoint_json TEXT DEFAULT '{}' NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_dfm_fabric_node UNIQUE (fabric_id, node_id), \n\tFOREIGN KEY(fabric_id) REFERENCES fabric (id) ON DELETE CASCADE, \n\tFOREIGN KEY(device_id) REFERENCES device (id) ON DELETE CASCADE\n)",
    'CREATE TABLE "thread_network" (\n\tid INTEGER NOT NULL, \n\tname TEXT NOT NULL, \n\tnetwork_name TEXT NOT NULL, \n\text_pan_id TEXT NOT NULL, \n\tpan_id TEXT NOT NULL, \n\tchannel INTEGER NOT NULL, \n\tmesh_local_prefix TEXT NOT NULL, \n\tnetwork_key TEXT NOT NULL, \n\tpskc TEXT, \n\tactive_timestamp INTEGER, \n\tborder_router_url TEXT NOT NULL, \n\tborder_agent_id TEXT, \n\tncp_version TEXT, \n\tlast_polled DATETIME, \n\tnotes TEXT, active_dataset_hex VARCHAR, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_thread_ext_pan_id UNIQUE (ext_pan_id)\n)',
    "CREATE INDEX ix_thread_network_ext_pan_id ON thread_network (ext_pan_id)",
    "CREATE TABLE \"ha_device_record\" (\n\tha_device_id TEXT NOT NULL, \n\tname TEXT DEFAULT ('') NOT NULL, \n\tmanufacturer TEXT DEFAULT ('') NOT NULL, \n\tmodel TEXT DEFAULT ('') NOT NULL, \n\tarea_name TEXT DEFAULT ('') NOT NULL, \n\tarea_id TEXT DEFAULT ('') NOT NULL, \n\tidentifiers_json TEXT DEFAULT '[]' NOT NULL, \n\tmatter_uid_set_json TEXT DEFAULT '[]' NOT NULL, \n\tfabric_id TEXT, \n\tnode_id INTEGER, \n\tserial TEXT, \n\tmatter_unique_id TEXT, protocol VARCHAR(16), sw_version VARCHAR, hw_version VARCHAR, connections_json VARCHAR DEFAULT '[]' NOT NULL, \n\tPRIMARY KEY (ha_device_id)\n)",
    "CREATE TABLE device_integration_data (\n\tid VARCHAR NOT NULL, \n\tdevice_id VARCHAR NOT NULL, \n\tintegration VARCHAR NOT NULL, \n\tpayload_json VARCHAR NOT NULL, \n\tretrieved_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(device_id) REFERENCES device (id) ON DELETE CASCADE, \n\tCONSTRAINT uq_did_device_integration UNIQUE (device_id, integration)\n)",
    "CREATE INDEX ix_device_integration_data_device_id ON device_integration_data (device_id)",
    "CREATE TABLE \"device\" (\n\tid TEXT NOT NULL, \n\tname TEXT NOT NULL, \n\troom TEXT, \n\tvendor TEXT, \n\tproduct TEXT, \n\tdevice_model TEXT, \n\tvendor_id INTEGER, \n\tproduct_id INTEGER, \n\tserial TEXT, \n\thardware_version TEXT, \n\tfirmware_version TEXT, \n\tnotes TEXT, \n\tpurchase_date DATE, \n\twarranty_until DATE, \n\tstatus TEXT DEFAULT 'active' NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tupdated_at DATETIME NOT NULL, \n\tmatter_unique_id TEXT, \n\tname_source TEXT DEFAULT 'generated' NOT NULL, \n\troom_source TEXT DEFAULT 'generated' NOT NULL, \n\tvendor_source TEXT DEFAULT 'generated' NOT NULL, \n\tproduct_source TEXT DEFAULT 'generated' NOT NULL, \n\tdevice_model_source TEXT DEFAULT 'generated' NOT NULL, \n\tvendor_id_source TEXT DEFAULT 'generated' NOT NULL, \n\tproduct_id_source TEXT DEFAULT 'generated' NOT NULL, \n\tserial_source TEXT DEFAULT 'generated' NOT NULL, \n\thardware_version_source TEXT DEFAULT 'generated' NOT NULL, \n\tfirmware_version_source TEXT DEFAULT 'generated' NOT NULL, \n\tmatter_unique_id_source TEXT DEFAULT 'generated' NOT NULL, \n\tnotes_source TEXT DEFAULT 'generated' NOT NULL, \n\tpurchase_date_source TEXT DEFAULT 'generated' NOT NULL, \n\twarranty_until_source TEXT DEFAULT 'generated' NOT NULL, \n\tstatus_source TEXT DEFAULT 'generated' NOT NULL, \n\tnetwork_type TEXT, \n\tnetwork_type_source TEXT DEFAULT 'generated' NOT NULL, \n\tmac_address TEXT, \n\tmac_address_source TEXT DEFAULT 'generated' NOT NULL, \n\tprotocol VARCHAR(16) DEFAULT 'matter', \n\thomekit_accessory_id VARCHAR, \n\tcommissioned_at VARCHAR, \n\tcommissioned_at_source VARCHAR DEFAULT 'generated' NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE INDEX ix_device_matter_unique_id ON device (matter_unique_id)",
    "CREATE INDEX ix_device_homekit_accessory_id ON device (homekit_accessory_id)",
)

_DROP_TABLES: tuple[str, ...] = (
    "device",
    "device_integration_data",
    "ha_device_record",
    "thread_network",
    "device_fabric_membership",
    "fabric",
    "device_link",
    "matter_node_record",
    "attachment",
    "property",
)


def upgrade() -> None:
    for stmt in _CREATE_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for name in _DROP_TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{name}"')
