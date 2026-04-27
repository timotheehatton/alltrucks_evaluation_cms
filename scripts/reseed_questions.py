#!/usr/bin/env python3
"""
Reseed the `question` collection from a German CSV.

Phases:
  1. Backup every existing question (all locales) to a timestamped JSON file.
  2. Validate the CSV up front (no writes).
  3. Operator confirmation gate.
  4. DELETE every question across all locales.
  5. POST every CSV row as a new question in its locale.

Talks to Strapi v5 over HTTPS using a Strapi API Token (Bearer auth).
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

CATEGORY_MAP = {
    'Diagnose': 'diagnostic',
    'Allgemeine Mechanik': 'general_mechanic',
    'Mechatronik Allgemein': 'general_mechanic',
    'Antriebsstrang': 'powertrain',
    'Elektrik': 'electricity',
    'Motor Abgas': 'engine_exhaust',
    'Abgassystem': 'engine_exhaust',
    'Motor Einspritzung': 'engine_injection',
    'Motoreinspritzung': 'engine_injection',
    'Druckluft-Bremsanlage Lkw': 'truck_air_braking_system',
    'Pneumatisches Bremssystem Lkw': 'truck_air_braking_system',
    'Anhänger-Bremsanlage': 'trailer_braking_system',
    'Anhänger Bremssystem': 'trailer_braking_system',
}

DIFFICULTY_MAP = {'1': 'easy', '2': 'medium', '3': 'hard'}

VALID_ANSWERS = {'choice_1', 'choice_2', 'choice_3', 'choice_4', 'choice_5'}

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = REPO_ROOT / 'scripts' / 'data' / 'question_de.csv'
BACKUP_DIR = REPO_ROOT / 'scripts' / 'backups'
ENV_FILES = [REPO_ROOT / '.env', REPO_ROOT / 'scripts' / '.env']

PAGE_SIZE = 100
HTTP_TIMEOUT = 60


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
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


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
        pagination = body.get('meta', {}).get('pagination', {})
        page_count = pagination.get('pageCount', 1)
        if page >= page_count:
            break
        page += 1
    return out


def fetch_locales(session, base_url, fallback_locales):
    """Best-effort locale discovery.

    /i18n/locales is an admin route — Strapi API tokens get 401 there.
    On any non-200 we fall back to the locales we observed in the backup,
    which is enough to detect a typo in the CSV's `local` column.
    """
    try:
        r = session.get(f'{base_url}/i18n/locales', timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            return [loc.get('code') for loc in r.json()], 'admin endpoint'
    except requests.RequestException:
        pass
    return sorted(fallback_locales), 'observed in existing data'


def write_backup(documents):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = BACKUP_DIR / f'questions-backup-{stamp}.json'
    with path.open('w', encoding='utf-8') as f:
        json.dump(documents, f, ensure_ascii=False, indent=2)
    return path


def parse_csv(csv_path):
    with csv_path.open('r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f, delimiter=';')
        return list(reader)


def build_payload(row, row_num, enabled_locales, target_locale):
    errors = []

    def get(col):
        v = row.get(col)
        return v.strip() if isinstance(v, str) else ''

    question = get('question')
    if not question:
        errors.append('missing question')

    choice_1 = get('choice_1')
    if not choice_1:
        errors.append('missing choice_1')

    choice_2 = get('choice_2')
    if not choice_2:
        errors.append('missing choice_2')

    category_raw = get('category')
    category = CATEGORY_MAP.get(category_raw)
    if not category:
        errors.append(f'unknown category {category_raw!r} (extend CATEGORY_MAP)')

    answer = get('answer')
    if answer not in VALID_ANSWERS:
        errors.append(f'invalid answer {answer!r} (expected one of choice_1..choice_5)')

    difficulty_raw = get('difficulty')
    difficulty = DIFFICULTY_MAP.get(difficulty_raw)
    if not difficulty:
        errors.append(f'invalid difficulty {difficulty_raw!r} (expected 1, 2, or 3)')

    locale = target_locale
    if locale not in enabled_locales:
        errors.append(f'locale {locale!r} not enabled in Strapi (enabled: {enabled_locales})')

    payload = {
        'question': question,
        'choice_1': choice_1,
        'choice_2': choice_2,
        'category': category,
        'anwser': answer,  # schema typo preserved per CLAUDE.md
        'difficulty': difficulty,
    }

    for opt in ('choice_3', 'choice_4', 'choice_5'):
        v = get(opt)
        if v:
            payload[opt] = v

    if answer not in payload and answer in {'choice_3', 'choice_4', 'choice_5'}:
        errors.append(f'answer points at {answer} but that column is empty')

    return payload, locale, errors


def validate_rows(rows, enabled_locales, target_locale):
    validated = []
    all_errors = []
    for i, row in enumerate(rows, start=1):
        payload, locale, errors = build_payload(row, i, enabled_locales, target_locale)
        if errors:
            all_errors.append((i, errors))
        else:
            validated.append((i, payload, locale))
    return validated, all_errors


def confirm(prompt):
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip() == 'DELETE'


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
                f'{deleted} documents deleted before failure; backup is intact.'
            )
        deleted += 1
        if deleted % 25 == 0:
            print(f'  ...deleted {deleted}/{len(document_ids)}')
    return deleted


def seed(session, base_url, validated):
    ok = 0
    failures = []
    for row_num, payload, locale in validated:
        try:
            r = session.post(
                f'{base_url}/api/questions',
                params={'locale': locale},
                json={'data': payload},
                timeout=HTTP_TIMEOUT,
            )
            if r.status_code not in (200, 201):
                failures.append((row_num, f'{r.status_code} {r.text[:300]}'))
                continue
            ok += 1
            if ok % 25 == 0:
                print(f'  ...seeded {ok}/{len(validated)}')
        except requests.RequestException as exc:
            failures.append((row_num, str(exc)))
    return ok, failures


def main():
    parser = argparse.ArgumentParser(description='Reseed Strapi questions from a German CSV.')
    parser.add_argument('--csv', type=Path, default=DEFAULT_CSV)
    parser.add_argument('--locale', default='de', help='Locale to write all rows to. Defaults to de.')
    parser.add_argument('--rows', help='Comma-separated 1-based row numbers to seed (e.g. "1,2,3,5"). Implies --skip-wipe.')
    parser.add_argument('--skip-wipe', action='store_true', help='Skip backup + delete; only seed.')
    parser.add_argument('--dry-run', action='store_true', help='Backup + validate only; no DELETE/POST.')
    parser.add_argument('--backup-only', action='store_true', help='Backup and exit.')
    parser.add_argument('--yes', action='store_true', help='Skip the interactive DELETE prompt.')
    args = parser.parse_args()

    load_dotenv(ENV_FILES)

    try:
        base_url = env_or_die('STRAPI_URL').rstrip('/')
        token = env_or_die('STRAPI_EMAIL_TOKEN')
    except Abort as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2

    session = session_for(token)

    print(f'TARGET:   {base_url}   (PRODUCTION)')

    skip_wipe = args.skip_wipe or bool(args.rows)
    row_filter = None
    if args.rows:
        try:
            row_filter = {int(x) for x in args.rows.split(',') if x.strip()}
        except ValueError:
            print(f'ERROR: --rows must be a comma-separated list of integers, got {args.rows!r}', file=sys.stderr)
            return 2

    documents = []
    if not skip_wipe:
        try:
            documents = fetch_all_questions(session, base_url)
        except Abort as exc:
            print(f'ERROR: {exc}', file=sys.stderr)
            return 2

        if not documents:
            print('WARNING: backup fetch returned 0 questions. Aborting before any writes.', file=sys.stderr)
            return 2

        backup_path = write_backup(documents)
        print(f'Backup:   wrote {len(documents)} questions to {backup_path}')

        if args.backup_only:
            return 0

    if not args.csv.is_file():
        print(f'ERROR: CSV not found at {args.csv}', file=sys.stderr)
        return 2

    observed_locales = {d.get('locale') for d in documents if d.get('locale')}
    if not observed_locales:
        observed_locales = {args.locale}
    enabled_locales, source = fetch_locales(session, base_url, observed_locales)
    print(f'Locales:  {enabled_locales} ({source})')

    rows = parse_csv(args.csv)
    validated, errors = validate_rows(rows, enabled_locales, args.locale)

    if errors:
        print(f'CSV validation failed for {len(errors)} of {len(rows)} rows. Aborting before any writes.', file=sys.stderr)
        for row_num, errs in errors:
            print(f'  row {row_num}: ' + '; '.join(errs), file=sys.stderr)
        return 2

    if row_filter is not None:
        validated = [v for v in validated if v[0] in row_filter]
        missing = row_filter - {v[0] for v in validated}
        if missing:
            print(f'ERROR: requested rows not found or invalid: {sorted(missing)}', file=sys.stderr)
            return 2

    locales_used = sorted({locale for _, _, locale in validated})
    if not skip_wipe:
        print(f'About to: DELETE all {len(documents)} questions across all locales')
    print(f'Then:     CREATE {len(validated)} new questions in locale={",".join(locales_used)}')

    if args.dry_run:
        print('Dry-run complete. No writes performed.')
        return 0

    if not skip_wipe:
        if not args.yes and not confirm('Type DELETE to proceed: '):
            print('Aborted by operator.')
            return 1

        document_ids = []
        seen = set()
        for d in documents:
            doc_id = d.get('documentId')
            if doc_id and doc_id not in seen:
                seen.add(doc_id)
                document_ids.append(doc_id)

        try:
            deleted = wipe(session, base_url, document_ids)
        except Abort as exc:
            print(f'ERROR: {exc}', file=sys.stderr)
            return 3

        print(f'Wiped {deleted} documents across all locales')

    ok, failures = seed(session, base_url, validated)
    print(f'Seeded {ok} / {len(validated)} rows ({len(failures)} failed)')
    if failures:
        for row_num, reason in failures:
            print(f'  row {row_num}: {reason}', file=sys.stderr)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
