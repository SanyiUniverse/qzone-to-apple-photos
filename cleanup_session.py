# SPDX-License-Identifier: GPL-3.0-only
"""Official QQ login plus explicit JSON commands; default is read-only.

Accepted commands: inventory, reconcile, delete_batch, quit. Delete commands also
need both startup declarations, an exact album/ID list and its selection digest.
No password or cookie value belongs in a command or configuration file.
"""
import argparse
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner', required=True, help='Your QQ account number')
    parser.add_argument('--output', required=True, type=Path, help='Existing private migration directory')
    parser.add_argument('--plan', type=Path, help='Frozen private cleanup plan')
    parser.add_argument('--allow-delete', action='store_true',
                        help='Declare that the account owner authorized these exact verified sources for removal')
    parser.add_argument('--recycle-bin-confirmed', action='store_true',
                        help='Declare you checked the current QQ account moves selected photos AND videos to its recycle bin')
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if not args.owner.isdigit() or int(args.owner) <= 0 or output.is_relative_to(Path(__file__).resolve().parent):
        parser.error('Use a numeric owner and a private output directory outside the source checkout')
    if args.allow_delete and not args.recycle_bin_confirmed:
        parser.error('--allow-delete also requires the current-account --recycle-bin-confirmed declaration')
    os.umask(0o077)
    sys.dont_write_bytecode = True
    os.environ['QZONE_OWNER'] = args.owner
    os.environ['QZONE_MIGRATION_OUTPUT'] = str(output)
    import migrate_session as migration
    import cleanup_adapter as adapter
    plan = args.plan or output / '迁移记录/QQ清理核验/精确白名单.json'
    adapter.configure(plan)
    ctx = adapter.context()  # Local proof/account validation before opening login.
    if ctx[0]['owner'].lstrip('0') != args.owner.lstrip('0'):
        raise ValueError('PLAN_BELONGS_TO_ANOTHER_ACCOUNT')
    session = migration.Session(args.owner)
    session._cleanup_authorized = args.allow_delete
    session._cleanup_recycle_confirmed = args.recycle_bin_confirmed
    try:
        migration.say(status='login_window_opening', operations_enabled=False)
        session._login_and_get_cookies()
        migration.say(status='READY', login_verified=True, delete_enabled=args.allow_delete,
                      no_cloud_change=True)
        for line in sys.stdin:
            command = None
            try:
                command = json.loads(line)
                if isinstance(command, dict) and command.get('cmd') == 'quit':
                    break
                result = adapter.handle(session, command)
                migration.say(**result)
            except Exception as exc:
                if isinstance(command, dict) and command.get('cmd') == 'delete_batch':
                    session._cleanup_blocked = True
                migration.say(status='stopped', error=type(exc).__name__,
                              action_not_retried=True, inspect_private_journal=True)
    finally:
        session.cookies.clear()
        session.session.cookies.clear()
        session.qzone_g_tk = ''
        session.session.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never print exception strings, request URLs, raw responses or cookies.
        print(json.dumps({'status': 'stopped', 'error': type(exc).__name__,
                          'no_credentials_logged': True}), flush=True)
        sys.exit(1)
