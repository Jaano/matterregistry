"""Thread 1.4 role names shown next to the Matter/Thread spec names."""

from __future__ import annotations

import pytest

from app.integrations.otbr.client import ThreadLink
from app.thread import thread_role_label


@pytest.mark.parametrize(
    ("role", "detail", "expected"),
    [
        ("Router", None, "Mesh Extender (Router)"),
        ("router", None, "Mesh Extender (router)"),
        ("REED", None, "Standby Mesh Extender (REED)"),
        ("Router", "RLOC16 0x1C00", "Mesh Extender (Router, RLOC16 0x1C00)"),
        ("Leader", None, "Leader"),
        ("Sleepy End Device", None, "Sleepy End Device"),
        ("Leader", "RLOC16 0x7000", "Leader (RLOC16 0x7000)"),
        (None, None, None),
        ("", None, None),
    ],
)
def test_thread_role_label(role, detail, expected):
    assert thread_role_label(role, detail) == expected


def _link(**over) -> ThreadLink:
    base = dict(
        network_id=1,
        network_name="n",
        channel=15,
        pan_id="0x1234",
        mesh_local_prefix="fd00::",
        border_router_url="http://otbr:8081",
        network_key="",
        rloc16=0x1C00,
        thread_role="router",
        parent_rloc16=None,
        link_margin=None,
        average_rssi=None,
        last_rssi=None,
        connection_time=None,
        frame_error_rate=None,
        link_quality=None,
    )
    base.update(over)
    return ThreadLink(**base)


def test_otbr_router_display_uses_thread_14_name():
    assert _link().role_display == "Mesh Extender (Router, RLOC16 0x1C00)"


def test_otbr_router_without_rloc_display():
    assert _link(rloc16=None).role_display == "Mesh Extender (Router)"


def test_otbr_end_device_display_unchanged():
    link = _link(thread_role="end_device", parent_rloc16=0x1C00)
    assert link.role_display == "End device of router 0x1C00"
