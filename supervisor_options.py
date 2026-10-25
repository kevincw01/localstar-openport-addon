"""Persist certificate paths, preserving all other user options."""

import asyncio
import contextlib
import json
import os
from pathlib import Path
import uuid

from aiohttp import ClientSession, ClientTimeout

SUPERVISOR_URL = 'http://supervisor'

def _save_local(directory, changes):
    """Support development containers without a Supervisor; never used in HA."""
    path = Path(directory) / "options.json"
    options = json.loads(path.read_text()) if path.exists() else {}
    temporary = path.with_name(uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(options | changes, stream)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


async def save_certificate_paths(directory, changes):
    if set(changes) != {'certificate_file', 'private_key_file'}:
        raise ValueError('Only certificate paths may be updated by this operation')
    token = os.environ.get('SUPERVISOR_TOKEN')
    if not token:
        await asyncio.to_thread(_save_local, directory, changes)
        return
    try:
        async with ClientSession(timeout=ClientTimeout(total=15),
                                 headers={'Authorization': f'Bearer {token}'}) as session:
            async with session.get(f'{SUPERVISOR_URL}/addons/self/info') as response:
                response.raise_for_status()
                info = await response.json()
                if info.get('result') != 'ok':
                    raise ValueError('Supervisor request failed')
                options = info['data']['options'] | changes
            async with session.post(f'{SUPERVISOR_URL}/addons/self/options',
                                    json={'options': options}) as response:
                response.raise_for_status()
                if (await response.json()).get('result') != 'ok':
                    raise ValueError('Supervisor request failed')
    except Exception:
        # HTTP errors and option responses can contain credentials. Return only
        # this fixed actionable message, never response bodies or tokens.
        raise RuntimeError('Could not save certificate paths in HA Configuration. '
                           'Check Supervisor availability and retry.') from None
