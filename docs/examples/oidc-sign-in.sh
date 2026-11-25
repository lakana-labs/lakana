#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-tos "Bring your team onto single sign-on", "Sign in through Keycloak"
# and "Sign in through Microsoft Entra ID". The pages show the regions.
#
# Not an example the runner starts (it needs a server with OIDC sign-in on and people linked to
# an identity provider): packages/lakana-server/tests/cli/test_oidc_docs.py runs every command of
# every region with the CLI against the real app, with OIDC sign-in on, `sam` linked and `kim` a
# linked administrator, and checks what each command did. The two `lakana login` regions run
# against the app's address instead of https://lakana.example.com, with the test playing the
# browser that confirms.

# docs:start login
lakana login --url https://lakana.example.com
# docs:end login

# docs:start login-device
lakana login --url https://lakana.example.com --device
# docs:end login-device

# docs:start window
lakana sign-in settings --token-window-days 14
# docs:end window

# docs:start offboard
lakana principal deactivate sam --yes
# docs:end offboard

# docs:start unlink
lakana principal unlink-provider sam --yes
# docs:end unlink

# docs:start refusals
lakana --output json audit list --event-type SIGN_IN_OIDC_REFUSED --limit 5
# docs:end refusals

# docs:start operator-recovery
lakana --profile team recovery create kim
# docs:end operator-recovery
