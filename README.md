# 🤖 أبو علي — Abu Ali

**مهندس DevOps معماري ذاتي + مبرمج كامل، يُدار بالكامل من جوالك (Android) عبر GitHub Issues.**

تكتب مشكلة أو أمراً في Issue من هاتفك → يقرأه أبو علي → يفكّر بمخّين
(**Groq** للسرعة + **Gemini** للسياق العميق) → ينفّذ فعلياً (ملفات، كوميت، Pull Request،
ونشر على Cloudflare إن لزم) → يردّ عليك في نفس الـ Issue → **يحفظ كل شيء في Supabase**
كي لا ينسى ولا يضيع شيء.

```
        📱 جوالك
           │  Issue / Comment / Cron
           ▼
   ┌───────────────────────┐
   │  GitHub Actions       │  ← المنظّم (Orchestrator)
   │  abu_ali_agent.yml    │
   └──────────┬────────────┘
              ▼
   ┌───────────────────────┐        ┌──────────────────────┐
   │  main.py              │◄──────►│  Supabase (Postgres) │
   │  المحرّك الذكي        │        │  tasks_log           │
   │  Groq ⇄ Gemini        │        │  system_memory       │
   │  GitHub ⇄ Cloudflare  │        │  interactions        │
   └──────────┬────────────┘        │  deployments         │
              │                     └──────────────────────┘
              ▼
   فرع → كوميت → Pull Request → (نشر Cloudflare) → رد في الـ Issue
```

---

## 🚀 الإعداد في 10 دقائق

### 0) ثبّت ملف الـ Workflow (خطوة واحدة)

الملف الجاهز موجود في `deploy/abu_ali_agent.yml`، ومكانه النهائي هو
`.github/workflows/abu_ali_agent.yml`. ثبّته بأحد طريقين:

**أ) بسطر واحد (توكن عنده صلاحية `workflow`):**

```bash
export MY_GITHUB_TOKEN=ghp_xxxxxxxx
python scripts/install_workflow.py            # يثبّته على الفرع الافتراضي
python scripts/install_workflow.py --check    # يتحقق فقط
```

**ب) من الجوال مباشرة:** GitHub App / المتصفح →
`Add file → Create new file` → اكتب المسار بالضبط
`.github/workflows/abu_ali_agent.yml` → الصق محتوى `deploy/abu_ali_agent.yml` → Commit.

> 💡 لماذا نسختان؟ لأن GitHub يمنع أي تطبيق/توكن بلا صلاحية `workflows` من الكتابة
> داخل `.github/workflows/`. فالسكربت يحلّ هذه المشكلة، والنسخة النصية تضمن أن
> الملف محفوظ في المستودع دائماً.

### 1) أضف الأسرار (Secrets)

افتح: **Settings → Secrets and variables → Actions → Secrets → New repository secret**

| الاسم (بالضبط) | من أين تجيبه | إلزامي؟ |
| --- | --- | --- |
| `MY_GITHUB_TOKEN` | PAT من `Settings → Developer settings → Tokens` بصلاحيات `repo` + `workflow` | ✅ |
| `GROQ_API_KEY` | `console.groq.com/keys` | ✅ |
| `GEMINI_API_KEY` | `aistudio.google.com/apikey` | ✅ |
| `SUPABASE_URL` | Supabase → Project Settings → API → Project URL | ✅ |
| `SUPABASE_SERVICE_ROLE_KEY` | نفس الصفحة → `service_role` (**سرّية جداً**) | ✅ |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare Dashboard → Workers → الحساب | اختياري |
| `CLOUDFLARE_API_TOKEN` | My Profile → API Tokens → `Workers Scripts:Edit` + `Pages:Edit` | اختياري |
| `CONTEXT7_API_KEY` | `context7.com` (توثيق مكتبات محدّث) | اختياري |

> ⚠️ **مهم:** لا يمكن أن يبدأ أي سرّ بـ `GITHUB_`. لذلك اسم التوكن هو
> `MY_GITHUB_TOKEN` بالضبط، وأبو علي يقرأه من البيئة كما هو.

### 2) أضف المتغيّرات (Variables) — اختيارية

**Settings → Secrets and variables → Actions → Variables → New repository variable**

| الاسم | الافتراضي | الوظيفة |
| --- | --- | --- |
| `AGENT_LANGUAGE` | `ar` | لغة كل النصوص البشرية |
| `LLM_PROVIDER_ORDER` | `groq,gemini` | من يفكّر أولاً |
| `AUTO_DEPLOY` | `true` | نشر Cloudflare تلقائياً عند تغيّر كود الحافة |
| `AUTO_MERGE` | `false` | دمج الـ PR تلقائياً (فعّله فقط إذا تثق 100%) |
| `MAX_FILES_PER_PR` | `10` | سقف الملفات في الطلب الواحد |
| `MAX_ISSUES_PER_RUN` | `2` | سقف المهام في التشغيل الواحد |
| `ABU_ALLOW_PROTECTED_WRITES` | `false` | السماح بتعديل `.github/workflows/**` |
| `SWEEP_COOLDOWN_HOURS` | `6` | أقل فاصل بين مهمتي صيانة |

### 3) أنشئ جداول Supabase (مرة واحدة)

Supabase → **SQL Editor** → الصق محتوى `db/schema.sql` → **Run**.

هذا ينشئ أربعة جداول لا تُحذف أبداً:

| الجدول | الوظيفة |
| --- | --- |
| `tasks_log` | سجل كل مهمة: المُشغّل، الحالة، الفرع، الكوميت، الـ PR، الخطأ، الزمن |
| `system_memory` | **الذاكرة طويلة المدى**: قرارات معمارية، تفضيلاتك، دروس، حقائق |
| `interactions` | كل حوار بينك وبينه على الـ Issues |
| `deployments` | كل عملية نشر على Cloudflare |

> إن نسيت هذه الخطوة، أبو علي يحاول إنشاءها بنفسه عبر RPC `exec_sql`، وإن فشل
> فإنه **لا يفقد البيانات**: يكتبها في `.abu-ali/outbox.jsonl` ويعيد دفعها لأول
> تشغيل ناجح.

### 4) تأكد أنه حي

Actions → **Abu Ali — Autonomous DevOps Agent** → **Run workflow** →
اكتب في `instruction`: `افحص لي صحة المستودع وأخبرني بالوضع` → Run.

---

## 📱 الاستخدام من الجوال (Android)

### الطريقة 1 — Issue جديدة (الأسهل من متصفح الجوال)

1. افتح المستودع في تطبيق GitHub → **Issues** → **New issue**
2. اكتب Title واضح + وصف مفصّل، والأفضل أضف ملفات مرفقة في الوصف (الصق الكود في الوصف)
3. **Labels**: أضف `task` أو `devops`
4. **Submit**

خلال ثوانٍ سيبدأ التشغيل، وسيصلك تعليقان: «بدأت…» ثم التقرير النهائي مع رابط الـ PR.

### الطريقة 2 — تعليق أمر على Issue موجودة

اكتب في التعليق:

```
/abu أضف endpoint للفحص الصحي في health/check.py
```

### 🇮🇶 الأوامر

| الأمر | ماذا يفعل |
| --- | --- |
| `/abu <أي طلب>` | مهمة حرة كاملة (يخطّط، ينفّذ، يفتح PR، يرد) |
| `/abu deploy worker <name>` | ينشر/يحدّث Cloudflare Worker بالكود الموجود في الـ Issue |
| `/abu deploy pages [project]` | يشغّل نشر Cloudflare Pages |
| `/abu cloudflare workers` | يسرد الـ Workers في حسابك |
| `/abu cloudflare projects` | يسرد مشاريع Pages |
| `/abu docs supabase rls` | يجيب توثيقاً حديثاً عبر Context7 MCP |
| `/abu search <نص>` | يبحث في الكود ويرجع المواضع |
| `/abu merge` | يدمج الـ PR المرتبط بالـ Issue |
| `/abu status` | يعرض آخر مهام هذا الـ Issue من Supabase |
| `/abu stop` | يتجاهل هذا التعليق (لا تنفيذ) |

> 🛡️ **حماية:** أوامر `/abu` من تعليق تحتاج صلاحية **write** أو أعلى. أي شخص بصلاحية
> قراءة فقط سيصله رد مهذّب بأن الأمر محجوب.

### الطريقة 3 — GitHub CLI / أي واجهة

```bash
gh workflow run abu_ali_agent.yml -f instruction="أضف اختبارات لوحدة الدفع"
gh workflow run abu_ali_agent.yml -f issue_number=42 -f dry_run=true
```

---

## 🧠 كيف يعمل (خط الأنابيب)

```
1. intake      اقرأ الـ Issue/التعليق + الأوامر + الملفات المرفقة
2. recall      ارجع من Supabase: ذاكرة طويلة المدى + تاريخ هذا الـ Issue
3. plan        Groq/Gemini يعيد JSON مُلزَم: intent، خطوات، ملفات، مخاطر، ذاكرة جديدة
4. execute     حلقة أدوات حقيقية (native tool calling):
               write_file · read_file · search_repo · context7_docs
               list_cloudflare_* · deploy_cloudflare · ask_human · submit
5. guard       فحص السلامة: مسارات محمية، سقوف الحجم، رفض الحذف بلا تأكيد
6. commit      فرع abu-ali/issue-<رقم>-<وصف> → كوميت ذرّي واحد
7. pr          Pull Request بوصف كامل (ماذا تغيّر ولماذا والمخاطر) و[skip ci]
8. deploy      ينشر على Cloudflare فقط إذا تغيّر كود الحافة
9. report      تعليق عربي منظّم في الـ Issue مع كل الروابط الحقيقية
10. remember   اكتب الدروس والقرارات في system_memory (upsert — لا حذف)
```

### متى يعمل تلقائياً؟

| الحدث | النتيجة |
| --- | --- |
| `issues: [opened, edited, labeled, reopened]` | مهمة جديدة/محدّثة |
| تعليق `/abu …` | أمر مباشر |
| `workflow_dispatch` | تشغيل يدوي (مع `dry_run`) |
| `schedule: 17 */6 * * *` | صيانة كل 6 ساعات: حصاد معرفة + إشارات خطر |
| `push` | لا مهمة (إلا إن كان مربوطاً بمهمة) — لتجنّب الدوران اللانهائي |

---

## 🧠 الذاكرة طويلة المدى (Supabase)

كل مهمة تنتهي بكتابة معرفة دائمة:

```json
{
  "kind": "architecture",
  "key": "health-endpoint-location",
  "content": "كل فحوصات الصحة تعيش في health/check.py وتُسجّل في app.py",
  "importance": 7,
  "tags": ["architecture", "ops"]
}
```

- **لا حذف أبداً** — يُحدَّث بـ `upsert` على `repo + key`.
- عند غياب الاتصال تُخزَّن الكتابات في `.abu-ali/outbox.jsonl` وتُدفع لاحقاً.
- أي دور جديد (member / manager) أو جدول جديد: يُضاف **فوق** الموجود، ولا يُزيل ميزة.

### استعلامات مفيدة في SQL Editor

```sql
-- آخر 20 مهمة
select created_at, issue_number, status, summary, pr_url
from tasks_log order by id desc limit 20;

-- ماذا يتذكّر عن هذا المستودع؟
select kind, key, content, importance
from system_memory order by importance desc, updated_at desc limit 30;

-- نسبة النجاح
select status, count(*) from tasks_log group by status order by 2 desc;
```

---

## 📡 تتبّع البناء (Build Tracking)

| ماذا | أين |
| --- | --- |
| التشغيل الحيّ | Actions → Run → Logs (بمستويات ملوّنة) |
| ملخص سريع | أسفل صفحة التشغيل: **Job Summary** (جدول مقاييس) |
| السجل الكامل | Artifacts → `abu-ali-log-<run_id>` |
| الحالة في قاعدة البيانات | Supabase → `tasks_log` |
| الردّ عليك | تعليق في الـ Issue + الـ PR |

لصق سريع في `Logs` ابحث عن: `stage:` (المرحلة)، `[tool]` (الأدوات)، `RUN SUMMARY` (الخلاصة).

---

## 🔐 الأمان — ما لن يفعله أبداً

| القاعدة | التنفيذ في الكود |
| --- | --- |
| لا حذف بيانات/جداول/ميزات | كل الـ DDL بـ `if not exists`، لا `DROP`، الحذف يتطلب أداة `delete_file` + `confirmed: true` |
| لا أسرار في الكود أو السجلات | قراءة من البيئة + فلتر `***REDACTED***` على كل سطر سجل |
| لا عبث بالـ CI | كتابة `.github/workflows/**` مرفوضة إلا بـ `ABU_ALLOW_PROTECTED_WRITES=true` |
| لا ملفات `.env` | مرفوضة تماماً |
| لا تفجير للحدود | سقوف: ملفات/حجم/مهام/جولات أدوات + `MAX_ISSUES_PER_RUN` |
| لا دوران لانهائي | كل تعليق من الوكيل يحمل علامة خفية `<!-- abu-ali:agent -->` فيتجاهل نفسه |
| لا ردود مخترعة | الروابط تُمرّر من نتائج حقيقية، ومزود فاشل يُستبدل تلقائياً |

---

## ☁️ نشر Cloudflare

- **Worker:** اطلب في الـ Issue كود العامل، ثم `/abu deploy worker my-api`،
  أو دع الوكيل يقرر ويستدعي أداة `deploy_cloudflare` بنفسه.
- **Pages:** `/abu deploy pages my-site` (يشغّل بناء من الفرع المربوط).
- كل نشر يُسجّل في جدول `deployments` مع الرابط والحالة.
- Workers تُحدَّث فقط — **لا تُحذف أبداً**.

---

## 🔌 MCP (Model Context Protocol)

أبو علي يتكلم MCP عبر **Streamable HTTP** (JSON-RPC 2.0) مع خوادم جاهزة:

| الخادم | الافتراضي | التشغيل |
| --- | --- | --- |
| GitHub | `api.githubcopilot.com/mcp/` | `MCP_ENABLE_GITHUB=true` |
| Supabase | `mcp.supabase.com/mcp` | `MCP_ENABLE_SUPABASE=true` + `SUPABASE_ACCESS_TOKEN` |
| Context7 | `mcp.context7.com/mcp` | مفعّل تلقائياً (توثيق المكتبات) |

لإضافة خادم أو تعديله، ضع متغيّر بيئة `MCP_SERVERS` بصيغة JSON:

```json
{
  "supabase": {
    "url": "https://mcp.supabase.com/mcp",
    "transport": "http",
    "apiKeyEnv": "SUPABASE_ACCESS_TOKEN",
    "enabled": true
  }
}
```

---

## 🧪 التطوير والاختبار محلياً

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python main.py --selftest        # 25 فحصاً بدون إنترنت
python main.py --status          # ما الذي تم ضبطه فعلاً
python main.py --dry-run --instruction "أضف اختبارات"
python tests/test_agent_offline.py   # تكامل كامل بمحاكاة GitHub/Supabase/LLM
python scripts/export_schema.py --check        # هل db/schema.sql محدّث؟
python scripts/install_workflow.py --offline   # هل نسختا الـ Workflow متطابقتان؟
```

للاختبار محلياً مع مفاتيح حقيقية: انسخ `.env.example` إلى `.env`.
(ملف `.env` مُستثنى من Git، والوكيل نفسه ممنوع من لمسه.)

---

## 🗂️ بنية الملفات

```
.
├── main.py                          # المحرّك الكامل (المخ، الذاكرة، الأدوات، النشر)
├── requirements.txt                 # التبعيات
├── ABU_ALI.md                       # دستور المشروع — يُحقن في كل prompt
├── db/schema.sql                    # جداول Supabase (مُولّد من main.py)
├── scripts/export_schema.py         # مولّد db/schema.sql
├── scripts/install_workflow.py      # مثبّت ملف الـ Workflow (بلا تبعيات)
├── tests/test_agent_offline.py      # اختبار تكامل بمحاكاة كاملة
├── deploy/abu_ali_agent.yml         # نسخة المصدر من ملف الـ Workflow
├── .github/workflows/abu_ali_agent.yml   # المنظّم الفعّال (يُثبّت بالسكربت)
├── .env.example                     # قائمة الأسرار للتشغيل المحلي
└── .abu-ali/outbox.jsonl            # صندوق صادر عند انقطاع Supabase (تلقائي)
```

---

## 🩺 حل المشاكل

| العرض | السبب المتوقع | الحل |
| --- | --- | --- |
| «PREFLIGHT: MY_GITHUB_TOKEN is missing» | السر غير مضاف أو باسم خاطئ | تأكد أن الاسم بالضبط `MY_GITHUB_TOKEN` |
| «Could not create tables automatically» | لا يوجد RPC `exec_sql` | شغّل `db/schema.sql` يدوياً مرة واحدة |
| «all LLM routes failed» | مفتاح/حد استهلاك | راجع `GROQ_API_KEY` و`GEMINI_API_KEY` |
| يعمل لكن لا PR | لا تغييرات مطلوبة أو كل الملفات مرفوضة | راجع `Skipped by safety policy` في السجل |
| لا يردّ على تعليقي | صلاحيتك أقل من write، أو التعليق من الوكيل نفسه | استخدم حسابك المالك أو `/abu stop` عكسياً |
| `refusing to allow ... without 'workflows' permission` عند الدفع | التوكن/التطبيق بلا صلاحية `workflows` | شغّل `python scripts/install_workflow.py` بتوكن عنده الصلاحية |
| الـ Workflow لا يشتغل على Issue | الملف غير موجود على **الفرع الافتراضي** | ثبّته على `main` (الأحداث المجدولة والإصدارات تُقرأ من الفرع الافتراضي فقط) |
| `AUTO_DEPLOY` مطلوب لكن لا نشر | أسرار Cloudflare ناقصة | أضف `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` |

---

## 🇬🇧 English quick reference

**Abu Ali** is a GitHub-native autonomous DevOps engineer you command from your phone.

```text
Issue (+ label `task`/`devops`)  →  plan (Groq) → generate (Gemini) → branch → commit
   → Pull Request → Cloudflare deploy → Arabic report comment → Supabase memory
```

• Secrets: `MY_GITHUB_TOKEN`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `CONTEXT7_API_KEY`
• Commands: `/abu <request>`, `/abu deploy worker|pages <name>`, `/abu cloudflare workers`,
`/abu docs <lib>`, `/abu search <text>`, `/abu merge`, `/abu status`, `/abu stop`
• Never destructive: additive-only changes, no table/feature deletion, secrets never logged,
CI files protected unless explicitly opted in.
• Install the workflow: `python scripts/install_workflow.py` (or paste `deploy/abu_ali_agent.yml`
into `.github/workflows/abu_ali_agent.yml` from the GitHub app — GitHub blocks non-`workflow` tokens there)
• Local: `python main.py --selftest` • `python tests/test_agent_offline.py`

---

_صُنع ليكون تحت إبهامك: مهمة من الجوال، وكود في المستودع. 🚀_
