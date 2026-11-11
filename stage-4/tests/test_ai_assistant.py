from harness.http import assert_status, new_key


def test_split_proposal_requires_confirmation_and_creates_requests(world):
    proposal_response = world.ada.post("/ai/proposals", json={
        "message": "Split 4,800.00 EUR dinner between me, bob and cy"})
    assert_status(proposal_response, 200)
    proposal = proposal_response.json()
    assert proposal["action"] == "split"
    assert proposal["provider"] == "deterministic_fallback"
    assert [s["amount"] for s in proposal["shares"]] == [160000, 160000, 160000]
    assert world.ada.get("/requests").json()["requests"] == []

    key = new_key()
    confirmed = world.ada.post(f"/ai/proposals/{proposal['proposal_id']}/confirm",
                               json={"confirm": True}, idempotency_key=key)
    assert_status(confirmed, 201)
    assert confirmed.json()["confirmed"] is True
    assert len(confirmed.json()["result"]["requests"]) == 2
    replay = world.ada.post(f"/ai/proposals/{proposal['proposal_id']}/confirm",
                            json={"confirm": True}, idempotency_key=key)
    assert_status(replay, 200)
    assert replay.json() == confirmed.json()
    assert len(world.ada.get("/requests").json()["requests"]) == 2


def test_ai_confirmation_is_user_scoped_and_requires_explicit_true(world):
    proposal = world.ada.post("/ai/proposals", json={
        "message": "Request 15.00 EUR from bob"}).json()
    rejected = world.ada.post(f"/ai/proposals/{proposal['proposal_id']}/confirm",
                              json={"confirm": False}, idempotency_key=new_key())
    assert rejected.status_code == 422
    foreign = world.bob.post(f"/ai/proposals/{proposal['proposal_id']}/confirm",
                             json={"confirm": True}, idempotency_key=new_key())
    assert foreign.status_code == 404


def test_ai_fallback_rejects_unknown_recipients_and_bad_amounts(world):
    unknown = world.ada.post("/ai/proposals", json={"message": "Request 20.00 EUR from stranger"})
    assert unknown.status_code == 422
    malformed = world.ada.post("/ai/proposals", json={"message": "Request lots from bob"})
    assert malformed.status_code == 422
    currency_mismatch = world.ada.post("/ai/proposals", json={"message": "Request ₹1,500 from bob"})
    assert currency_mismatch.status_code == 422
