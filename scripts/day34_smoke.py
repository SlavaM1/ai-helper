"""Explicitly opt-in live Day 34 demos; consumes configured provider tokens.

python3 scripts/day34_smoke.py --live --base http://127.0.0.1:3010
Only managed workspaces are edited. Never calls source apply or git push.
"""

import argparse
import json
import time
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--base', default='http://127.0.0.1:3000')
    parser.add_argument('--output', default='/tmp/opencode/day34-demos')
    parser.add_argument('--scenario', action='append', type=int, choices=[1, 2, 3], help='Run only selected scenarios')
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    def request(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(args.base + '/api/file-agent' + path, data=data,
                                     headers={'Content-Type': 'application/json', 'Origin': args.base})
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)

    def wait(task):
        print('Started', task['id'], flush=True)
        deadline = time.monotonic() + 1500
        while task['status'] in {'queued', 'running'}:
            if time.monotonic() > deadline:
                raise TimeoutError(task['id'])
            time.sleep(3)
            task = request('/tasks/' + task['id'])
        return task

    prompts = [
        'Изучи структуру контроллеров приложения, прочитай связанные сервисы и создай документ docs/api-overview.md с описанием основных HTTP endpoints и роли сервисного слоя. Достаточно представительного обзора 3–5 контроллеров и 1–2 связанных сервисов с реальными файлами и строками, не полного каталога.',
        'Проверь архитектурное правило: контроллеры не должны напрямую обращаться к JPA repositories. Самостоятельно исследуй несколько реальных контроллеров и репозиториев, сформируй компактный Markdown-отчёт docs/controller-repository-report.md с найденными нарушениями, файлами и строками. Отделяй эвристику от подтверждённых зависимостей.',
        'Проанализируй реализацию авторизации и создай ADR docs/adr/0001-authentication-architecture.md с описанием текущего архитектурного решения. Достаточно 3–6 ключевых связанных компонентов, используй RAG для архитектурного контекста и текущий код для фактов. Отдели факты от предложений; создай компактный документ, явно ограничь охват.',
    ]
    results = []
    for number, prompt in enumerate(prompts, 1):
        if args.scenario and number not in args.scenario:
            continue
        task = wait(request('/tasks', {'prompt': prompt}))
        (output / f'demo-{number}.json').write_text(json.dumps(task, ensure_ascii=False, indent=2))
        diff = request('/tasks/' + task['id'] + '/diff')
        (output / f'demo-{number}.redacted.patch').write_text(diff['diff'])
        summary = {'scenario': number, 'id': task['id'], 'status': task['status'],
                   'read_files': task['read_files'], 'changes': task['changes'],
                   'usage': task['usage'], 'error': task['error']}
        if task['workspace'].get('artifact'):
            replay = wait(request('/tasks/' + task['id'] + '/replay', {}))
            replay_diff = request('/tasks/' + replay['id'] + '/diff')
            summary.update(replay_id=replay['id'], replay_status=replay['status'],
                           replay_diff_equal=diff['diff'] == replay_diff['diff'])
            (output / f'demo-{number}-replay.json').write_text(json.dumps(replay, ensure_ascii=False, indent=2))
        results.append(summary)
        (output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
