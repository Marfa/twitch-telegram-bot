# Правила проекта для Qoder

## Обязательное чтение правил

Правила проекта хранятся в [`.cursor/rules/`](.cursor/rules) в формате `.mdc` (тело — обычный Markdown, frontmatter с `description`/`globs`/`alwaysApply` — специфика Cursor, читать только тело).

**Перед началом любой задачи прочитай все `.mdc`-файлы из `.cursor/rules/` и следуй им.** Frontmatter-поле `globs` указывает, к каким файлам правило относится — применяй правило, если трогаешь перечисленные пути (если `globs` нет — правило общее).

Список на текущий момент:

- `active-subscription-gate.mdc`
- `api-license-compliance.mdc`
- `beta-feature-branch.mdc`
- `cleanup-artifacts.mdc`
- `commit.mdc`
- `docker.mdc`
- `flow-nav-scenarios.mdc`
- `git-commits.mdc`
- `group-chat-replies.mdc`
- `igdb-dumps.mdc`
- `postgresql.mdc`
- `pre-post-push-github.mdc`
- `premium-feature-copy.mdc`
- `python-telegram-bot.mdc`
- `readme.mdc`
- `reply-keyboard.mdc`
- `security-devsecops.mdc`
- `self-check.mdc`
- `twitch-client-secret.mdc`
- `user-flow-map.mdc`
- `user-guide.mdc`
- `viaduct-c4.mdc`
- `vps-db-query.mdc`
- `wizard-step-removal.mdc`
- `anti-overengineering.mdc`

Если список и каталог разойдутся — каталог главнее; список просто подстраховка, что ничего не пропущено.

## Хуки

Хуки из `.cursor/hooks.json` (например, cleanup после git commit) в Qoder автоматически не выполняются. Если правило ссылается на хук — запускай соответствующий скрипт из `.cursor/hooks/` вручную в нужный момент.
