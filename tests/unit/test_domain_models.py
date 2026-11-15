from foresight.domain.models import MessageRecord, OpKind, WriteOp

EXPECTED_OP_KINDS = {
    "MESSAGE": "message",
    "MESSAGE_INSERT_ONLY": "message_insert_only",
    "MESSAGE_EDIT": "message_edit",
    "MESSAGE_DELETE": "message_delete",
    "REACTION": "reaction",
    "REACTION_REMOVE": "reaction_remove",
    "USER": "user",
    "CHANNEL": "channel",
    "GUILD": "guild",
    "ATTACHMENT": "attachment",
    "ATTACHMENT_STATUS": "attachment_status",
    "ANALYSIS_STATUS": "analysis_status",
    "ANALYSIS": "analysis",
}


def test_message_record_defaults():
    record = MessageRecord(
        message_id=1,
        guild_id=2,
        channel_id=3,
        author_id=4,
        content="hi",
        message_type=0,
        created_at="2026-11-11T00:00:00+00:00",
        captured_at="2026-11-11T00:00:00+00:00",
    )
    assert record.edited_at is None
    assert record.edit_count == 0
    assert record.deleted_at is None
    assert record.reply_to_message_id is None
    assert record.analysis_status == "pending"


def test_write_op_holds_kind_and_payload():
    op = WriteOp(OpKind.MESSAGE, {"a": 1})
    assert op.kind == "message"
    assert op.payload == {"a": 1}


def test_op_kind_constants_are_unique():
    values = [
        value
        for name, value in vars(OpKind).items()
        if not name.startswith("_") and isinstance(value, str)
    ]
    assert len(values) == len(set(values))


def test_op_kind_values_are_exactly_the_expected_ones():
    actual = {
        name: value
        for name, value in vars(OpKind).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    assert actual == EXPECTED_OP_KINDS
