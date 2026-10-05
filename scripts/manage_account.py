"""Trusted server-side account administration; never expose this script as an API."""
import argparse
import getpass
import asyncio
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select, delete
from app.db.database import AsyncSessionLocal, engine
from app.models.accounts import Account, AccountSession

async def main(args):
    async with AsyncSessionLocal() as db:
        account=(await db.execute(select(Account).where(Account.email==args.email.strip().lower()))).scalar_one_or_none()
        if not account: raise SystemExit('Account not found. Register in the app first.')
        if args.reset_password:
            password=getpass.getpass('New password (12–128 characters): ')
            if not 12<=len(password)<=128: raise SystemExit('Password must be 12–128 characters.')
            if password!=getpass.getpass('Confirm password: '): raise SystemExit('Passwords do not match.')
            from app.api.accounts import password_hash
            account.password_hash=password_hash(password)
        if args.role: account.role=args.role
        if args.disable: account.enabled=False
        if args.enable: account.enabled=True
        if args.revoke_sessions or args.disable or args.role or args.reset_password:
            await db.execute(delete(AccountSession).where(AccountSession.account_id==account.id))
        await db.commit()
        print('Account updated. Existing access tokens expire within 15 minutes; revoked sessions cannot refresh.')
    await engine.dispose()

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('email');p.add_argument('--role',choices=['user','responder','admin']);p.add_argument('--revoke-sessions',action='store_true');p.add_argument('--reset-password',action='store_true')
    g=p.add_mutually_exclusive_group();g.add_argument('--disable',action='store_true');g.add_argument('--enable',action='store_true')
    asyncio.run(main(p.parse_args()))
