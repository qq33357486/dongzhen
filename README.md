# 洞真

> 窈兮冥兮，其中有精；其精甚真，其中有信。——《道德经·二十一章》

模型指纹验真：向待测模型发出整数生成挑战，比对回答的数字分布指纹，判定其**家族与候选模型**——验的是"货是不是真的"。名字取自道藏三洞「洞真部」，洞彻真源。

## 为什么开这个分支

上游 [xqy2006/ModelTrace](https://github.com/xqy2006/ModelTrace) 创造了这套数字指纹归因方法，本文档与代码的根基来自它，致谢。但上游已停止维护，于是有了本分支：

1. **指纹库不再更新**——新模型一个没有，国产模型（GLM 等）也不在库。本分支建了录入管线（[docs/enrollment.md](docs/enrollment.md)），新模型 36 题即可入指纹库；GLM 三型号已实测（flash/flashx 指纹近孪生、与 GPT/Claude 互为独立族，够格入库）。
2. **AI 开箱即用**——页面内藏给 AI 的使用提示词，**把网址丢给任意 AI Agent，它自己就会用**：出题、拿去问目标模型、交卷、拿判定，全程无需人类教。接口为无状态两件套（`/challenge` 出题、`/submit` 判卷），另有 `/probe` 服务代调路径。

其余改造：判定口径接受"族"概念（同族不可分时如实并列候选而非硬判）、FastAPI 服务化 + CI 镜像流水线、key 全程脱敏。

## 网页（极简）

打开首页就是三件事：指纹库加载提示 → **三次挑战 / 重新生成 / 计算归因概率** → 结果。归因计算全部在浏览器本地完成，页面零外呼、零密钥。

页面内藏给 AI 的接口说明（人不可见、AI 抓取可读），纯文本版在 `GET /llms.txt`。

## API 服务（service 分支）

| 端点 | 用途 |
|---|---|
| `POST /challenge` | 得到题目（题面信息编进自包含 challenge_id，无状态，24h 有效） |
| `POST /submit` | 提交答案拿判定（族 + 族内候选列表，同族不可分时如实并列） |
| `POST /probe` / `/probe_batch` | 服务代调探针（AI 自助模式之外的自动化路径） |
| `GET /history` / `/health` | 历史留底（key 脱敏）/ 健康检查 |

判定口径：判出 astra=正常没降智；判出 luna 族=降智；库外/分差过小=unknown（不硬判）。规则集中在 `api/config.py`。

完整接口文档：[docs/api.md](docs/api.md)

## 部署

- 镜像：`ghcr.io/qq33357486/dongzhen`（生产只拉镜像，不在服务器上构建；CI 推 `service` 分支自动构建）
- 本地：`docker build -t dongzhen:service .` + [docker-compose.yml](docker-compose.yml)
- 环境变量：`DZ_API_TOKEN`（Bearer 鉴权，未设置则放行）、`DZ_CHALLENGE_SECRET`（challenge_id 签名密钥，缺省回退 API_TOKEN）

### 论坛服务器更新部署（实测路径：网关中转）

论坛机（106.55.180.108，部署目录 `/data/docker/dongzhen`）**没有 crane 也没有 skopeo**（旧记忆作废），apt 直连装不上；docker daemon 无代理，`docker pull` ghcr 拉不动。实测可行：**经网关机（175.178.191.8）中转**——网关有 `/usr/local/bin/ghcr-pull`（skopeo + 7890 代理预检）。两台机均用密钥 `~/.ssh/tencent_new_key`：

```bash
# 1) 网关拉镜像（进网关 docker）
ssh -i ~/.ssh/tencent_new_key root@175.178.191.8 "ghcr-pull ghcr.io/qq33357486/dongzhen:service"

# 2) 网关导出
ssh -i ~/.ssh/tencent_new_key root@175.178.191.8 "docker save ghcr.io/qq33357486/dongzhen:service | gzip > /tmp/dz.tgz"

# 3) 经本机两跳传到论坛机
scp -i ~/.ssh/tencent_new_key root@175.178.191.8:/tmp/dz.tgz /tmp/dz.tgz
scp -i ~/.ssh/tencent_new_key /tmp/dz.tgz root@106.55.180.108:/tmp/dz.tgz

# 4) 论坛机加载 + 重建容器
ssh -i ~/.ssh/tencent_new_key root@106.55.180.108 "gunzip -c /tmp/dz.tgz | docker load"
ssh -i ~/.ssh/tencent_new_key root@106.55.180.108 "cd /data/docker/dongzhen && docker compose up -d"

# 5) 清理 tar（论坛机磁盘仅 ~9G，用完即删）
rm /tmp/dz.tgz
ssh -i ~/.ssh/tencent_new_key root@175.178.191.8 "rm /tmp/dz.tgz"
ssh -i ~/.ssh/tencent_new_key root@106.55.180.108 "rm /tmp/dz.tgz"
```

红线：不动论坛机 `.env` / 证书 / nginx；`docker compose up -d` 只在 `/data/docker/dongzhen` 目录执行（该机还跑别的服务）。改生产 compose 前先备份。

镜像一致性对账：`docker load` 会丢 `RepoDigests`，用 image ID 核——`docker inspect --format '{{.Id}}'` 网关与论坛机应一致（2026-11-13 实测 `7de0d352af15…` 两侧一致）。

### 部署验收清单（全过才算完）

```bash
# 论坛机本机（8100 只绑回环，公网走 nginx 前门）
docker ps --filter name=dongzhen-api --format '{{.Status}}'                        # Up (healthy)
docker inspect dongzhen-api --format '{{.RestartCount}}'                           # 0（非重启循环）
curl -s http://127.0.0.1:8100/health                                               # "models":23
curl -s http://127.0.0.1:8100/ | grep -o '窈兮冥兮，其中有精'                       # 命中新诗句
curl -s http://127.0.0.1:8100/ | grep -c ModelTrace                                # 0（旧名残留必须为 0）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8100/llms.txt            # 200

# 外网前门（http 会 301 跳 https，直接验 https）
curl -s https://dz.duojie.games/health                                             # "models":23
curl -s https://dz.duojie.games/ | grep -o '窈兮冥兮，其中有精'                     # 命中
curl -s https://dz.duojie.games/ | grep -c ModelTrace                              # 0
curl -s -o /dev/null -w '%{http_code}\n' https://dz.duojie.games/llms.txt          # 200
```

备选路径：论坛机用 `apt-get -o Acquire::http::Proxy="http://127.0.0.1:7890" …` 装 skopeo 后本地 `skopeo copy + docker load` 理论可行但**未实测**；docker 容器里跑 skopeo 属最后手段，均不如网关中转稳。

## 新模型指纹录入

36 题（12 环境 × 3）官方/可信直连源采集 → 候选文件人工审核 → 显式并入 → 混淆回归验证。SOP：[docs/enrollment.md](docs/enrollment.md)

## 文档

- [架构理解](docs/architecture.md) —— 挑战生成 / 指纹特征 / 比对算法 / 两级判定
- [族与分支鉴别研究](docs/family-branch-research.md) —— 库内混淆矩阵、统计功效、升级路线
- [API 文档](docs/api.md)
- 运维 CLI：`mt_cli.py`（3 题并发探针）
