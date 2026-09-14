# 7-Minute Demo Guide

**Project:** Online Exam Proctoring System Using Deep Learning
**Institution:** KNUST — Department of Computer Science
**Format:** Live walkthrough, no slides. One narrative: *register → build → sit → catch → review.*

> The companion [PRESENTATION_GUIDE_30MIN.md](PRESENTATION_GUIDE_30MIN.md) is the full defence deck.
> This document is the short live demo only.

---

## 1. Feature Inventory

Everything the system does, grouped by who uses it. The demo touches the **bolded** items.

### Accounts & access control
- Three roles — Administrator, Teacher, Student — with separate sign-in doors (`/login/student/`, `/login/teacher/`, `/login/admin/`)
- **Student registration with a live webcam face scan** — the captured frame becomes the identity reference for every future exam
- Student ID card upload with **automatic OCR verification**; failures fall through to a human admin queue
- Teacher registration with staff ID upload — **account stays locked until an admin approves it**
- Sign in with either username or email (case-insensitive)
- Middleware gates: an unapproved teacher can only reach `/pending-approval/`; a student with an unverified ID can only reach `/id-review/`

### Administrator
- **Teacher Approvals** — review the staff ID, then approve or reject
- **Student ID Reviews** — manually adjudicate IDs that OCR could not read
- **Exam Approvals** — read the paper before students ever see it, then approve or reject
- Live exam controls: **freeze** a running exam, or **grant extra time** to everyone
- Proctoring Audit — read-only view of every flagged session across all courses
- All Users, All Courses, All Exams; revoke or reactivate any account

### Teacher — authoring
- Course list scoped to the courses they own
- **Question Bank Builder** — a three-pane editor: outline, question editor, metadata
- Three question types — **multiple choice, true/false, short answer** — with the answer editor switching live as you change the type
- Per-question marks, section, difficulty, Bloom's level, module tags, explanation
- **Bulk CSV import** with a downloadable sample and per-row error reporting
- Exam builder: duration, total and passing marks, attempt limit, question shuffling, scheduling window
- **Per-exam strictness**: Not strict (no proctoring) · Level 1 (5-strike tolerance) · Level 2 (zero tolerance)
- **Submit for admin approval → publish → generate access codes** (single-use or capped, with expiry)

### Student — sitting the exam
- Dashboard of available exams and a **Redeem Access Code** page
- **Face-match identity check before the Begin button unlocks** (ArcFace, cosine similarity ≥ 0.35)
- Enforced fullscreen lockdown; server-authoritative timer that cannot be tampered with client-side
- Live webcam proctor feed showing FACE VERIFIED and remaining violations
- **Full-screen strike warnings** the student must acknowledge, with an option to file a dispute
- Disconnect grace period — a dropped connection pauses rather than fails the attempt
- My Marks, and a per-attempt result page

### Proctoring engine
- Browser uploads a webcam frame **every second** to a Celery worker
- **YOLOv5** object detection: phone, book, notes, secondary devices
- **YOLOv8-pose** for head turn, plus **MediaPipe iris gaze** for eyes-off-screen
- Detects: `phone`, `book`, `notes`, `absent`, `multiple_faces`, `face_obstructed`, `tab_switch`, `focus_lost`, `exit_fullscreen`
- False-positive damping: look-away must be **sustained ~5.5 s**; multi-person needs consecutive frames; per-type cooldowns
- Every strike stores a **snapshot with bounding boxes and pose keypoints**, plus a short **clip of lead-up frames**
- WebSocket pushes warnings to the student instantly; auto-terminates on the final strike

### Teacher — review
- **Flagged Sessions**: session list → AI timeline → machine-readable logs → **CV snapshot inspector with pose-skeleton and object-box overlays**
- Adjudicate each violation: uphold or dismiss, with a note the student sees
- **Disqualify / nullify an attempt** (score forced to 0, reason shown to the student)
- **Exam Results roster** — every submission with score, status and flags
- **Click a student to open their full question-by-question responses in a modal**
- Manual grading queue for short answers, with feedback that rolls into the final score

---

## 2. Before You Present (do this 15 minutes early)

The demo fails if you try to create things live. **Pre-stage the data, then narrate finished work.**

**Two browser windows, side by side.** Use a normal window for the teacher and a **private/incognito** window for the student — otherwise you spend your seven minutes logging in and out.

Pre-stage checklist:

- [ ] Server running: `python manage.py runserver`
- [ ] Admin, teacher, and student accounts all exist and are **already approved**
- [ ] The student registered **with a real face scan** — the exam face-match compares against it
- [ ] One question bank with **4–6 questions, including at least one short answer**
- [ ] One exam: approved, **published**, schedule open now, **strictness Level 1**, access code generated and written on your hand
- [ ] **One already-completed attempt from an earlier run that has violations on it** — this is what you show in the review section. Do not rely on catching violations live and then reviewing them; there isn't time.
- [ ] A phone in your pocket as the detection prop
- [ ] Camera and fullscreen permissions already granted; hard-refresh both windows (Cmd+Shift+R)
- [ ] Close every unrelated tab; silence notifications

---

## 3. The Run of Show

Timings are cumulative. If you fall behind, cut Segment 2 to a single sentence — it is the most compressible.

### 0:00 – 0:40 · The problem

**Do:** Stay on your face, nothing on screen yet.

**Say:**
> "Universities run more exams online every year. Browser lockdown can stop a student switching tabs — but it cannot see the phone under the desk, the notes beside the laptop, or the second person off-camera. That's a visibility gap, and it's what this project closes. I've built a complete examination platform with deep-learning proctoring embedded in it. Let me show you the whole lifecycle in about six minutes."

---

### 0:40 – 1:50 · Identity and access control

**Do:** Student window → `/register/student/`. Point at the form. **Do not fill it in.**

**Say:**
> "Everything starts with identity. A student registers with their index number, uploads their KNUST ID card, and — this is the important part — takes a live face scan right here in the browser. That scan isn't decoration. It becomes the biometric reference we match against every time they sit an exam."

**Do:** Point at the ID card field.

**Say:**
> "The ID card is read automatically with OCR. If it can't be read confidently, the student isn't rejected — they're routed to an administrator queue for a human decision."

**Do:** Switch to the admin window → sidebar → **Teacher Approvals**, then **Student ID Reviews**.

**Say:**
> "Teachers can't self-serve either. A new teacher account is locked until an administrator reviews their staff ID — until then, middleware physically prevents them reaching any teaching page. Three roles, three separate doors, and nobody gets in on their own say-so."

---

### 1:50 – 3:20 · The teacher builds the exam

**Do:** Teacher window → sidebar → **Question Banks** → open your prepared bank → **Builder**.

**Say:**
> "This is the question bank builder. Outline on the left, editor in the middle, metadata on the right."

**Do:** Click a multiple-choice question. Then, in the metadata panel, **change Type to Short Answer** and let the audience watch the answer editor swap instantly.

**Say:**
> "Three question types. Watch the editor — when I switch this to a short answer, the multiple-choice options disappear and I get a reference-answer field instead. Multiple choice and true/false are marked automatically; short answers go into a manual queue for the teacher, because a human should mark prose."

**Do:** Mention the Bulk Import tab without clicking into it. Then sidebar → **Exams** → open your exam.

**Say:**
> "Questions can also be bulk-imported from CSV. Once the bank is ready, the teacher assembles an exam — duration, marks, a scheduling window, and the proctoring strictness. This one is Level 1: a five-strike tolerance. Level 2 is zero tolerance, and there's a relaxed mode for practice tests."

**Do:** Point at the approval status and the access code.

**Say:**
> "A teacher cannot publish unilaterally. The exam goes to an administrator, who reads the paper first and approves it. Only then can the teacher publish and hand out an access code. That's a real academic control, not a technical one."

---

### 3:20 – 4:30 · The student sits the exam

**Do:** Student window → **Redeem Access Code** → type the code → start the exam.

**Say:**
> "The student redeems their code. Now the important part — they cannot start yet."

**Do:** Let the ID check run. Wait for **IDENTITY CONFIRMED**.

**Say:**
> "The webcam opens and the system matches this live face against the scan taken at registration, using ArcFace embeddings. It's a cosine similarity against a threshold. Until that passes, the Begin button stays locked. This closes the most obvious attack on remote exams: someone else sitting the paper."

**Do:** Click **Enter fullscreen & begin**. Point at the lockdown bar and the floating proctor feed.

**Say:**
> "We're now in enforced fullscreen. The timer is authoritative on the server, so editing the client clock does nothing. And that's the live proctor feed — face verified, violations remaining, five of five."

---

### 4:30 – 5:40 · Catching a violation live

**Do:** Hold your phone up clearly in the webcam frame. Hold it steady — give the second-by-second capture time to fire. Wait for the strike overlay.

**Say (while waiting):**
> "Every second, the browser sends a frame to a background worker. YOLOv5 looks for foreign materials — phones, books, notes. A pose model and an iris-gaze model watch head turn and eye direction."

**Do:** When the strike card appears, read it out and click **Acknowledge**.

**Say:**
> "There it is. Detected, logged, and pushed back over a WebSocket in near real time. The student must acknowledge it — and can dispute it, which the teacher sees later. Note it warns rather than instantly failing: look-away has to be sustained for several seconds before it counts, so a glance at your notes-free desk doesn't end your degree. On the fifth strike, the exam terminates automatically."

**Do:** Submit the exam (or just stop here and switch windows).

**If the detection does not fire within ~15 seconds:** stop waiting. Say *"Detection is confidence-thresholded and my lighting here isn't ideal — let me show you a session where it did fire"* and go straight to the next segment using your pre-staged attempt. **Rehearse this sentence.** It costs you nothing if you say it calmly.

---

### 5:40 – 6:40 · The teacher reviews the evidence

**Do:** Teacher window → sidebar → **Flagged Sessions** → open your pre-staged flagged attempt.

**Say:**
> "This is where it pays off. The teacher gets a timeline of every violation in the session."

**Do:** Click a **CV snapshot** to open the inspector. Toggle **Pose skeleton** and **Object boxes**.

**Say:**
> "And for each strike, the actual frame — with the model's bounding box and the pose skeleton drawn over it. The teacher isn't asked to trust a score; they see the evidence the model saw. They can uphold or dismiss each one, and in the worst case nullify the attempt outright."

**Do:** Sidebar → **Exams** → open the exam → **View Results**. Click a student row to open the responses modal.

**Say:**
> "Separately, here's the academic side: every submission with its score, and clicking a student opens their full paper — what they answered, what was correct, and their short answers waiting to be marked by hand. Integrity evidence and academic marking, in one place."

---

### 6:40 – 7:00 · Close

**Say:**
> "So: verified identity before the exam, enforced lockdown during it, deep-learning detection of the physical materials a browser can't see, and reviewable evidence afterwards. Not a detection script — a complete examination platform with proctoring built into its spine. Happy to take questions."

---

## 4. If Something Breaks

| Failure | Recovery |
|---|---|
| Camera won't open | Check the site's camera permission; a second tab holding the camera will block it. Fall back to the pre-staged flagged session. |
| Face match won't pass | The student account must have a real face scan from registration. If it fails twice, say the fault budget admits-and-flags for staff review — *that is a designed behaviour, not a bug*. |
| Phone not detected | Hold it closer, flatter to the camera, and steadier. After ~15 s, cut to the pre-staged session. |
| Fullscreen prompt blocked | Click once anywhere on the page first, then retry the Begin button. |
| Anything else | Switch to the flagged session and narrate from stored evidence. Never debug live. |

---

## 5. Fast Answers to Likely Questions

**"Isn't this just tab-switch detection?"**
No — that part is trivial and we do it too. The contribution is visual detection of physical materials: phones, books, notes, a second person. That's what a browser genuinely cannot see.

**"What about false positives?"**
Layered damping. Look-away must be sustained for several seconds; multi-person needs consecutive frames; every detection has a confidence threshold and a cooldown. And critically, a strike is evidence for a human, not an automatic verdict — the teacher upholds or dismisses it.

**"Privacy?"**
We store violation snapshots and logs, not continuous video. Students consent before the exam, and media is access-controlled per role.

**"Can they just hide the phone off-camera?"**
Partial occlusion is a real limitation and I state it as one. One-second sampling shrinks the blind window, but no camera-based system is complete. It raises the cost of cheating; it doesn't claim to eliminate it.

**"Does it need a GPU?"**
No. It runs on CPU on ordinary student hardware, which was a deliberate constraint — a proctoring system only useful with a GPU isn't deployable at a Ghanaian university.

**"Who can overrule the AI?"**
Always a human. Teachers adjudicate violations on their own courses; administrators have a read-only audit across all of them.
