# PR #519 coverage fix report

Commit: `test(security): cover the sign-in branch's remaining refusal and race paths` (tests only, no src change).

All listed lines are covered by unit-tier tests (no Docker, no integration tier needed).

## links.py (test_identity_links.py)
- 690-692: `test_two_invites_for_one_name_at_the_same_moment_name_the_next_step` (commit raises IntegrityError once; AlreadyExistsError names next step; no link left).
- 841: `test_invite_refuses_an_overlong_display_name`.
- 906: `test_recovery_of_an_unknown_name_names_the_next_step`.
- 1117->1123: `test_closing_a_link_that_was_withdrawn_meanwhile_audits_nothing_twice` (`_withdraw_if_open` finds no row; no second WITHDRAWN event).
- 1337-1338: `test_invite_whose_name_is_taken_in_the_race_window_is_closed` (check bypassed, flush hits unique name; link withdrawn, audit reason name_taken).
- 1463 (and 1462-1465): `test_name_of_a_link_follows_its_kind_and_survives_a_missing_principal`.
- 1488: `test_withdraw_for_principal_commits_when_asked`.

## passkeys.py (test_identity_passkeys.py)
- 205: `test_a_passkey_name_over_100_characters_is_refused_with_a_shorter_suggestion`.
- 652-654: `test_two_servers_creating_the_challenge_key_at_once_end_with_one_key` (winner row inserted before the savepoint flush).
- 693-694: `test_a_sign_in_answer_whose_client_data_is_not_json_is_refused_as_expired`.
- 785: `test_registration_options_for_a_person_who_is_gone_say_so`.
- 895-897: `test_a_credential_id_stored_between_the_check_and_the_insert_is_refused`.
- 1222: `test_a_challenge_of_the_wrong_size_is_not_one_of_ours` (direct `open_challenge`).
- 1248-1249: `test_cross_origin_client_data_is_refused_and_unreadable_data_too` (direct `_cross_origin`).
- 1268-1269: `test_unreadable_registration_answer_gives_empty_client_data_which_is_refused` (direct `_registration_client_data`).
- Note: 1248-1249 and 1268-1269 are fail-closed defences behind the library's own parsing, so unreachable through the service; kept (they refuse on doubt) and unit-tested directly rather than removed.

## browser_guard.py / browser_session.py (server tests)
- 141->140: `test_cookies_that_are_not_the_sessions_do_not_make_browser_traffic`.
- 292: `test_a_path_without_a_leading_slash_moves_below_the_public_origin`.
- browser_session 121: `test_cookie_authenticates_api_v1_behind_a_path_prefix` (root_path "/lakana").

## Verification
- `--cov-branch` term-missing: browser_guard 100 %, browser_session 100 %; links.py only 501 missing, passkeys.py only 176, 474-475 missing locally (all three were not in CI's miss list, i.e. covered by the integration tier).
- 290 passed over the four test files; ruff check, ruff format --check, ty check packages, lint-imports (6 kept) pass.
- Duplication ratchet: 1193 lines / 61 clones vs baseline 1195 — fell (not from this commit, src untouched); baseline lowering via `--update` is left for the owner. Ran jscpd locally with output in ~/.cache.
- Not run: whole-repo coverage, bandit (tests only).
