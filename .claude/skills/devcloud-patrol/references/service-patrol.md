# 服务暴露取证与处置手册

服务侧的判定没有脚本捷径：端口相同，性质可能完全不同。本手册给的是取证动作、判定证据和处置模板；每轮巡检按此执行，结论由当轮证据决定。

## 取证清单（每个非回环监听一行）

宿主机进程：

```bash
# 列出非回环监听（0.0.0.0、::、内网 IP）
ss -tlnp | awk '$4 !~ /127\.0\.0\.1|\[::1\]/'
# 属主 / 启动时间 / 父进程 / 命令行
ps -o pid,user,lstart,ppid,cmd -p <PID>
# 进程属于哪个项目
readlink /proc/<PID>/cwd
# 父进程还活着吗（变成孤儿 = nohup/断连残留的强证据）
ps -o cmd= -p "$(ps -o ppid= -p <PID>)"
```

容器：

```bash
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
docker port <name>
docker inspect --format \
  '{{.Name}} started={{.State.StartedAt}} restart={{.HostConfig.RestartPolicy.Name}} protect={{index .Config.Labels "mole.protect"}}{{index .Config.Labels "devcloud.protect"}} compose={{index .Config.Labels "com.docker.compose.project.working_dir"}}' \
  <name>
```

红线：不读取、不回显 `/proc/<pid>/environ` 和 `docker inspect` 的 `Env` 字段——里面有口令和 secret。暴露与弱口令的判定只看监听面和配置存在性，不需要把 secret 打进报告或日志。

## 残留判定

同时满足以下证据组合的判"开发残留候选"，处置前仍需 owner 确认：

- 监听地址是非回环；
- 启动时间明显早于当前工作会话（小时/天级），或父进程已是孤儿；
- cmdline 是 dev server / 临时服务特征（vite、next dev、webpack-dev-server、turbo、jest --watch、`python -m http.server` 等）；
- cwd 指向某个项目目录，且该项目近期无活动（佐证，不单独定案）。

"正在被使用"的信号（默认判保留，除非 owner 明确放手）：父进程是活跃 shell；启动时间在几分钟内；owner 说在用。拿不准就归"未知"，问 owner。

## 本机待处理记录

本机服务与核验细节见 `.local/devcloud/publication-backup/.claude/skills/devcloud-patrol/references/service-patrol.md`。

## 常见端口指纹

| 端口 | 典型属主 | 默认态度 |
|---|---|---|
| 5173 | vite | 残留候选（存在 CVE 暴露面） |
| 3000 / 8080 | next dev / webpack / 通用 web | 残留候选 |
| 5432 / 3306 | postgres / mysql | 高风险，只走 owner 命令 |
| 6379 | redis | 高风险（默认无口令） |
| 27017 | mongo | 高风险 |
| 9000 | minio | 高风险 |
| 9229 | node inspector | 残留候选（等同 RCE 暴露面） |
| 8000 / 8888 | http.server / jupyter | 残留候选 |

指纹只是起点，归属以取证清单的当轮证据为准。

## 处置命令模板

**杀残留进程（只按精确 PID）：**

```bash
# 处置前当场重验，PID 会复用：
[[ "$(tr '\0' ' ' </proc/<PID>/cmdline)" == *"<特征子串>"* ]] || { echo "PID 已换身份，回到取证"; exit 1; }
kill <PID>                              # 先 TERM
sleep 5; kill -0 <PID> 2>/dev/null && kill -KILL <PID>
ss -tlnp | grep -F ':<端口>' && echo "端口仍在，未闭环" || echo "端口已消失"
```

不用 `pkill -f` / `killall` 做批量匹配——模式会咬中别的进程。

**停临时容器：**

```bash
docker inspect --format '{{index .Config.Labels "mole.protect"}}{{index .Config.Labels "devcloud.protect"}}' <name>  # 输出含 true 则不碰
docker stop <name>
# 属于 compose 项目且 owner 确认可弃的：在其 compose working_dir 下执行 docker compose down
# 数据卷（-v / volume）不随 down 隐式删除，另行按证据决定
```

**收敛监听面（保留服务、去掉暴露）：**

- dev server：改 `--host 127.0.0.1` / 配置文件后重启进程；
- postgres：pg_hba 收敛 + `ALTER USER` 换口令后 reload。

每项处置完成即回报三件事：动作、前后证据（端口在 → 不在 / 占用前后）、以及适用的回收量。全部处置结束回到 SKILL.md 第 4 步记账。
