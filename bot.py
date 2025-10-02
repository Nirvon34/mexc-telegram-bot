# .github/workflows/keep-awake.yml
name: Keep Render Awake

on:
  schedule:
    - cron: "*/12 * * * *"   # каждые 12 минут (UTC)
  workflow_dispatch: {}       # ручной запуск из вкладки Actions

jobs:
  ping:
    runs-on: ubuntu-latest
    steps:
      - name: Ping Render (HEAD with retries, no-fail)
        env:
          URLS: |
            https://mexc-telegram-bot-1xd7.onrender.com/
            https://mexc-telegram-bot-1xd7.onrender.com/health
        run: |
          set +e
          ok=0
          i=1
          while [ $i -le 3 ]; do
            echo "Attempt $i ..."
            for u in $URLS; do
              CODE=$(curl -s -I -L -o /dev/null -w "%{http_code}" \
                     --connect-timeout 10 --max-time 20 \
                     -A "gh-actions-keep-awake" "$u")
              echo "  $u → HTTP $CODE"
              if [ "$CODE" -ge 200 ] && [ "$CODE" -lt 500 ]; then
                ok=1
              fi
            done
            [ $ok -eq 1 ] && echo "OK" && exit 0
            sleep 15
            i=$((i+1))
          done
          echo "No success after retries, but don't fail the job."
          exit 0
