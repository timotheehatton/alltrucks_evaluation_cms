#!/usr/bin/env python3
"""
One-shot migration: i18n questions  →  per-country questions.

Strapi side change: drop i18n from the `question` collection and add a
required `country` enum field (FR/ES/PL/DE/IT). After this migration the
quiz fetches questions by country instead of by locale.

Per product decision: keep ONLY the German locale of the existing
questions, drop everything else. French content will be uploaded later
from a separate CSV via the existing `reseed_questions.py`.

Workflow (operator runs each phase):

    # 1. Before changing the schema — pull every question (all locales)
    #    to a timestamped backup AND a normalized "DE only" export.
    python scripts/migrate_questions_to_country.py --phase=export

    # 2. Delete every question (across every locale). Backup is on disk.
    python scripts/migrate_questions_to_country.py --phase=wipe

    # 3. Operator edits `src/api/question/content-types/question/schema.json`
    #    (remove i18n config + add `country` enum) and restarts Strapi.

    # 4. Re-import the DE export, each row written with country=DE.
    python scripts/migrate_questions_to_country.py --phase=import

    # 5. Operator imports the French CSV via reseed_questions.py later.

Talks to Strapi v5 over HTTPS using a Strapi API Token (Bearer auth).
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

PAGE_SIZE = 100
HTTP_TIMEOUT = 60
SOURCE_LOCALE = 'de'
TARGET_COUNTRY = 'DE'

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / 'scripts' / 'data'
BACKUP_DIR = REPO_ROOT / 'scripts' / 'backups'
EXPORT_FILE = DATA_DIR / 'questions_de_export.json'
ENV_FILES = [REPO_ROOT / '.env', REPO_ROOT / 'scripts' / '.env']

# Fields we copy from the DE-locale snapshot into the new schema. Other
# attributes (image, category, anwser, difficulty) are reused as-is.
TEXT_FIELDS = ['question', 'choice_1', 'choice_2', 'choice_3', 'choice_4', 'choice_5']


class Abort(Exception):
    pass


def load_dotenv(paths):
    for path in paths:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding='utf-8').splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_or_die(name):
    value = os.environ.get(name)
    if not value:
        raise Abort(f'Environment variable {name} is required but not set.')
    return value


def session_for(token):
    s = requests.Session()
    s.headers.update({
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
    })
    return s


def fetch_all_questions(session, base_url):
    """Page through every question across every locale, with image populated."""
    out = []
    page = 1
    while True:
        params = {
            'locale': '*',
            'pagination[page]': page,
            'pagination[pageSize]': PAGE_SIZE,
            'populate': 'image',
        }
        r = session.get(f'{base_url}/api/questions', params=params, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            raise Abort(f'GET /api/questions page={page} failed: {r.status_code} {r.text[:500]}')
        body = r.json()
        out.extend(body.get('data', []))
        meta = body.get('meta', {}).get('pagination', {})
        if page >= meta.get('pageCount', 1):
            break
        page += 1
    return out


def write_backup(documents):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = BACKUP_DIR / f'questions-full-backup-{stamp}.json'
    with path.open('w', encoding='utf-8') as f:
        json.dump(documents, f, ensure_ascii=False, indent=2)
    return path


def normalize_de_rows(documents):
    """Pick every document whose locale == 'de' and emit a row that the import
    phase can replay against the new (no-i18n) schema."""
    rows = []
    seen_doc_ids = set()
    for doc in documents:
        if doc.get('locale') != SOURCE_LOCALE:
            continue
        # Dedupe in case a document appears multiple times in the API response.
        doc_id = doc.get('documentId')
        if doc_id and doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(doc_id)

        row = {field: doc.get(field) for field in TEXT_FIELDS if doc.get(field)}
        row['category'] = doc.get('category')
        row['anwser'] = doc.get('anwser')  # schema typo preserved
        row['difficulty'] = doc.get('difficulty')

        image = doc.get('image')
        if image:
            # In Strapi v5 the populated image object carries `id`. Re-using
            # that ID re-links the new question to the existing media without
            # re-uploading.
            row['image_id'] = image.get('id')

        rows.append(row)
    return rows


def confirm(prompt, expected):
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip() == expected


def wipe(session, base_url, document_ids):
    deleted = 0
    for doc_id in document_ids:
        r = session.delete(
            f'{base_url}/api/questions/{doc_id}',
            params={'locale': '*'},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code not in (200, 204):
            raise Abort(
                f'DELETE /api/questions/{doc_id} failed: {r.status_code} {r.text[:500]}. '
                f'{deleted} documents deleted before failure; backup is intact on disk.'
            )
        deleted += 1
        if deleted % 25 == 0:
            print(f'  ...deleted {deleted}/{len(document_ids)}')
    return deleted


def import_rows(session, base_url, rows):
    ok = 0
    failures = []
    for i, row in enumerate(rows, start=1):
        payload = {k: v for k, v in row.items() if k != 'image_id' and v is not None}
        payload['country'] = TARGET_COUNTRY
        if row.get('image_id'):
            payload['image'] = row['image_id']

        r = session.post(
            f'{base_url}/api/questions',
            json={'data': payload},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code not in (200, 201):
            failures.append((i, f'{r.status_code} {r.text[:300]}'))
            continue
        ok += 1
        if ok % 25 == 0:
            print(f'  ...imported {ok}/{len(rows)}')
    return ok, failures


def phase_export(session, base_url):
    print(f'TARGET:   {base_url}')
    documents = fetch_all_questions(session, base_url)
    if not documents:
        raise Abort('No questions found — refusing to write an empty export.')

    backup_path = write_backup(documents)
    print(f'Backup:   {len(documents)} documents (all locales) → {backup_path}')

    rows = normalize_de_rows(documents)
    if not rows:
        raise Abort(
            f'Backup OK but no documents have locale={SOURCE_LOCALE!r}. '
            f'Refusing to write an empty DE export.'
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with EXPORT_FILE.open('w', encoding='utf-8') as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f'Export:   {len(rows)} DE-locale rows → {EXPORT_FILE}')
    print()
    print('NEXT:     `--phase=wipe` to delete every question, then change the')
    print('          schema, restart Strapi, and run `--phase=import`.')


def phase_wipe(session, base_url, skip_prompt):
    print(f'TARGET:   {base_url}')

    if not EXPORT_FILE.is_file():
        raise Abort(
            f'Refusing to wipe — no export found at {EXPORT_FILE}. '
            f'Run --phase=export first.'
        )

    documents = fetch_all_questions(session, base_url)
    if not documents:
        print('Nothing to delete.')
        return

    doc_ids = []
    seen = set()
    for d in documents:
        doc_id = d.get('documentId')
        if doc_id and doc_id not in seen:
            seen.add(doc_id)
            doc_ids.append(doc_id)

    print(f'About to delete {len(doc_ids)} documents across every locale.')
    if not skip_prompt and not confirm('Type DELETE to proceed: ', 'DELETE'):
        print('Aborted by operator.')
        return

    deleted = wipe(session, base_url, doc_ids)
    print(f'Wiped {deleted} documents across all locales')


def phase_import(session, base_url):
    print(f'TARGET:   {base_url}')

    if not EXPORT_FILE.is_file():
        raise Abort(f'No export found at {EXPORT_FILE}. Run --phase=export first.')

    rows = json.loads(EXPORT_FILE.read_text(encoding='utf-8'))
    if not rows:
        raise Abort('Export file is empty — nothing to import.')

    # Smoke-test the new schema: try one POST and bail out cleanly if Strapi
    # rejects the `country` field (operator forgot to apply the schema).
    print(f'Importing {len(rows)} rows with country={TARGET_COUNTRY}...')
    ok, failures = import_rows(session, base_url, rows)
    print(f'Imported {ok} / {len(rows)} rows ({len(failures)} failed)')

    if failures:
        print('Failures (first 5 shown):')
        for row_num, err in failures[:5]:
            print(f'  row {row_num}: {err}')
        if any('country' in err for _, err in failures):
            print(
                '\nHINT: at least one failure mentions `country`. Did you remove '
                'the i18n config from the schema and add the country enum field '
                'before restarting Strapi?'
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        '--phase',
        choices=['export', 'wipe', 'import'],
        required=True,
        help='Which step to run. Always run in order: export → wipe → (schema change) → import.',
    )
    parser.add_argument('--yes', action='store_true', help='Skip the interactive DELETE prompt on --phase=wipe.')
    args = parser.parse_args()

    load_dotenv(ENV_FILES)
    try:
        base_url = env_or_die('STRAPI_URL').rstrip('/')
        token = env_or_die('STRAPI_EMAIL_TOKEN')
    except Abort as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2

    session = session_for(token)

    try:
        if args.phase == 'export':
            phase_export(session, base_url)
        elif args.phase == 'wipe':
            phase_wipe(session, base_url, skip_prompt=args.yes)
        elif args.phase == 'import':
            phase_import(session, base_url)
    except Abort as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
