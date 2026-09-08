from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from app.media import MediaValidator
from app.provider import FailureCode, PollResult, ProviderOutput, ProviderStatus
from app.provider_callbacks import ProviderCallbackService
from app.provider_completion import ProviderCompletionService
from app.provider_execution_context import AttemptContextService
from app.provider_polling import ProviderPollingPolicy, ProviderPollingService
from app.provider_settlement import ProviderSettlementService
from app.provider_submission import ProviderSubmissionService


def test_collaborators_construct_without_database_storage_or_network_calls():
    session = Mock(side_effect=AssertionError("constructor must not open a session"))
    storage, routes, claims, recorder, receiver = (Mock() for _ in range(5))
    context = AttemptContextService(session)
    submission = ProviderSubmissionService(
        storage, routes, claims, timedelta(minutes=5), session, recorder
    )
    ProviderPollingService(session, ProviderPollingPolicy(), lambda: datetime.now(UTC))
    ProviderCallbackService(session)
    ProviderCompletionService(
        session,
        storage,
        receiver,
        context,
        submission,
        ProviderSettlementService(),
        recorder,
        MediaValidator(),
    )
    session.assert_not_called()
    assert not any(item.mock_calls for item in (storage, routes, claims, recorder, receiver))


def test_real_provider_cannot_bypass_receiver_with_embedded_bytes():
    session, storage, receiver, contexts, submission, recorder = (Mock() for _ in range(6))
    completion = ProviderCompletionService(
        session,
        storage,
        receiver,
        contexts,
        submission,
        ProviderSettlementService(),
        recorder,
        MediaValidator(),
    )
    failure = Mock()
    completion.fail_attempt = failure
    context = SimpleNamespace(provider_code="runpod")
    output = ProviderOutput(content=b"embedded", media_type="video/mp4")
    result = PollResult(status=ProviderStatus.SUCCEEDED, output=output)
    completion.finish_output(context, output, result)
    assert failure.call_args.args[1].code == FailureCode.OUTPUT_MISSING
    session.assert_not_called()
    assert not storage.mock_calls
    assert not receiver.mock_calls
