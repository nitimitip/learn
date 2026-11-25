APPASSIST TEST CASE AGENT - README
========================================================================
A Microsoft 365 Copilot agent (Agent Builder, used from Microsoft Teams
or the Copilot app) that acts as a senior test lead. It reads the user's
requirement documents and writes professional positive, negative and
boundary test cases into an Excel workbook in the AppAssist theme.

It is not tied to an AppAssist module or database: there is no JSON
draft, no database ID and nothing to import into AppAssist. The output
is the Excel file only.

FILES
------------------------------------------------------------------------
  TestCase_agent_config.txt          the agent WITHOUT a skill: setup,
                                     instructions (Part 2), rules and the
                                     workbook code (Part 6), all in one
                                     knowledge file
  skills/appassist-testcase.zip      the skill (upload package)
  skills/appassist-testcase/         the same skill unpacked, for reading
                                     and maintenance
  Sample_TestCase_input_Requirements.txt
                                     fictional combined BRD + FRS + HLD
                                     document with deliberate gaps
  Sample_Test_Cases.xlsx             the workbook the agent should produce
                                     from that sample input
  make_testcase_builder_md.py        maintenance: regenerates the skill's
                                     references/builder-code.md from its
                                     scripts (not part of the upload)
  Sample_TestCase_spec.py            that sample as a Python dict, for
                                     maintenance and testing only (NOT in
                                     the skill: Copilot accepts only .md
                                     files in references/)
  TestCase_README.txt                this file

WHAT THE AGENT DOES
------------------------------------------------------------------------
  Users do not need to know how to prompt: uploading a document with
  "create test case document" is enough. The agent reads the document
  first and asks only what it cannot find there - every doubt, in plain
  English.

  1. Takes whatever document is uploaded (BRD, FRS, SRS, HLD, user
     stories, use cases, combined spec) - one is enough; it never asks
     for another. (No document: asks for one in one line.)
  2. Reads ALL of it (tables, appendices and notes included), lists
     every requirement and sorts the content by test level: business
     processes -> "UAT (BRD)"; detailed rules, fields, messages, roles
     -> "System Test (FRS)"; named system interfaces -> "SIT-API (HLD)".
     One document can feed all three sheets.
  3. Asks every doubt as a numbered question (up to 8 per message), with
     follow-up rounds until nothing is unclear.
  4. Confirms: what it will build, which tests it leaves out because a
     question was not answered, and "Shall I prepare the Excel file?".
  5. On yes it builds: 3-4 requirements per batch, rebuilding the real
     workbook after each batch, until every requirement is covered. If
     it has to stop, it gives the in-progress workbook and asks you to
     reply "continue".
  6. Gives the Excel file with a short summary and the left-out tests.

  The chat is only for questions and the confirmation; test cases are
  never written in chat. The end result is always the Excel file.

  It never makes up data. When you cannot answer a question, the tests
  that depend on it are LEFT OUT (never "To be confirmed", never a
  guess) and listed against the question on the Clarifications sheet.
  If every test of a requirement depends on it, the RTM shows that
  requirement as "Dropped". Answer later and the agent adds those tests
  and rebuilds the workbook.

  It never writes generic cases. Every title, precondition, step, test
  data and expected result is specific to that one case (real screen,
  field, role, value, state, message). The workbook code itself
  (build_workbook, in the skill and in Part 6 of the config) REFUSES to
  build when it finds placeholder wording ("Verify requirement for
  FR_REQ_01", "Validate mandatory / invalid handling", "User logged in
  with valid role", "Open related screen", "Perform action with test
  data", "Submit and observe result", "Requirement based data", "System
  behaves as per specification", ...), a title, steps or expected
  result copied between cases, the same preconditions or test data on
  most cases, or "To be confirmed" / an unanswered question in a case.

THE WORKBOOK
------------------------------------------------------------------------
  Dashboard          project details, 18 KPI tiles (total, executed,
                     pass, fail, blocked, not run, pass rate, progress,
                     positive / negative / boundary, defects, requirement
                     coverage, critical failures, open questions), status
                     by level, priority and category tables, two charts
  UAT / System Test / SIT-API sheets
                     TC ID, Req ID, Module, Title, Test Type, Category,
                     Priority, Preconditions, Test Steps, Test Data,
                     Expected Result, Actual Result, Status, Defect ID,
                     Executed By, Execution Date, Remarks
  RTM                requirement -> test cases, with coverage status
  Clarifications     open questions for the document authors

  Every Dashboard and RTM figure is a formula. Testers fill in Status,
  Actual Result and Defect ID (or add rows, up to row 1004) and the
  figures update by themselves. Status, Type, Category and Priority are
  drop-down lists.

SETUP OPTION A - WITH THE SKILL  (recommended)
------------------------------------------------------------------------
  The skill carries the workbook code as a real script plus a checker,
  so the workbook is always built exactly as designed.

  1. Agent Builder > create agent > Configure > Skills > Add.
  2. Upload skills/appassist-testcase.zip (the zip, not SKILL.md alone).
  3. Code interpreter: ON.  Web search: OFF.  No knowledge file needed.
  4. Name:  AppAssist Test Case Agent
     Description and conversation starters: copy from Part 1 of
     TestCase_agent_config.txt.
  5. Instructions (paste):
  ----------------------------------------------------------------------
  You are the AppAssist Test Case Agent: a principal test lead with 30
  years of experience. For any request about test cases, test scenarios,
  UAT / SIT scripts or an RTM, use the appassist-testcase skill and
  follow its Non-negotiables exactly:
  1. The uploaded document is enough, whatever it is. Never ask for an
     FRS, SRS, HLD or any other document.
  2. Read the whole document carefully - every section, table and
     appendix - and list every requirement before writing anything.
  3. In chat only ask questions: every doubt as a numbered question,
     follow-up rounds until nothing is unclear. Never assume or guess.
  4. A question the user cannot answer: leave out the tests that depend
     on it and list them - never "To be confirmed", never a guess.
  5. When all doubts are settled, confirm what you will build and what
     you leave out, and ask "Shall I prepare the Excel file?". Build
     only after the user agrees.
  6. Set up the builder from the skill's references/builder-code.md and
     write the cases 3-4 requirements at a time, rebuilding the real
     AppAssist workbook after each batch, until every requirement is
     covered. If you must stop, give the in-progress workbook, say which
     requirements are done and which are next, and ask the user to
     reply "continue".
  7. The end result is always the Excel file from the skill's builder.
     Never write test cases in chat, never build a "framework" or
     self-coded workbook, never say the document is too large.
  8. Every case is specific (real screen, field, role, value, state,
     message) - never generic wording such as "Verify requirement for
     ...", "User logged in with valid role", "Open related screen",
     "Perform action with test data", "Requirement based data" or
     "System behaves as per specification".
  You only write test cases; send other requests to the matching
  AppAssist agent.
  ----------------------------------------------------------------------

  Skills in Agent Builder may still be a preview feature in your tenant.
  If Configure shows no Skills section, use option B.

SETUP OPTION B - WITHOUT A SKILL
------------------------------------------------------------------------
  1. Instructions: paste Part 2 of TestCase_agent_config.txt (between
     BEGIN INSTRUCTIONS and END INSTRUCTIONS).
  2. Knowledge: upload TestCase_agent_config.txt.
  3. Code interpreter: ON.  Web search: OFF.
  Limitation: Copilot reads a knowledge file in chunks. If it does not
  pick up all of Part 6, the agent may build a workbook that differs
  from the AppAssist design. Option A does not have this risk.

  Do not use both options on one agent.

INSIDE THE SKILL
------------------------------------------------------------------------
  SKILL.md                         workflow and rules
  references/builder-code.md       the two scripts as copy blocks + SHA-256:
                                   Copilot's code interpreter cannot see
                                   scripts/, so the agent installs them
                                   into its sandbox from here and proves
                                   the copy (HASH OK + SELFTEST OK).
                                   GENERATED - do not edit by hand
  references/conversation-guide.md talking to non-expert users: bare
                                   "create test cases" uploads, every
                                   doubt asked, follow-up rounds, the
                                   confirmation before the build,
                                   left-out tests, loose replies, no /
                                   wrong / unreadable document
  references/quality-standard.md   expert document analysis, what every
                                   column must carry, wording the checker
                                   refuses, bad -> good, self-review
  references/document-guide.md     content -> test level; one document
                                   feeds every level it supports
  references/test-design.md        techniques, test types, how each column
                                   is written, priority, examples
  references/worked-example.md     one requirement from document to
                                   analysis note to cases (fictional)
  references/workbook-layout.md    sheets, lists, KPI definitions, theme
  references/checklist.md          checks before the file is given
  scripts/appassist_testcase.py    checks the test cases and builds the
                                   workbook; refuses on any ERROR (wrong
                                   list value, bad TC ID, more than one
                                   Req ID, unknown requirement, empty
                                   column, unknown Q-nn, a requirement
                                   with no tests and no dropped reason)
  scripts/tc_builder.py            draws the workbook; its quality()
                                   gate (generic wording, Req ID in a
                                   title, copied title / steps / expected
                                   result, same precondition or test data
                                   on most cases, "To be confirmed" / an
                                   unanswered Q-nn) runs inside
                                   build_workbook, so no setup can skip it

  references/ holds only .md files (Copilot rejects other file types
  there); Python lives in scripts/ only.

  The agent passes the test cases to the helper as a Python dict inside
  its own script. No JSON or other intermediate file is used.

REQUIREMENTS IN THE COPILOT SANDBOX
------------------------------------------------------------------------
  Python with the openpyxl package (standard in the Copilot code
  interpreter). If the agent cannot run Python it says the code
  interpreter must be turned on and stops; it does not put the test
  cases in chat.

MAINTENANCE
------------------------------------------------------------------------
  * The workbook code exists twice: skills/appassist-testcase/scripts/
    tc_builder.py and Part 6 of TestCase_agent_config.txt. Change both
    together.
  * The skill's references are Parts 3, 4, 5 and 7 of the config file
    (Part 4 = test-design.md + quality-standard.md as sections 7-11;
    Part 8 = conversation-guide.md).
    Change both together.
  * After ANY change to scripts/appassist_testcase.py or
    scripts/tc_builder.py run  python make_testcase_builder_md.py  (in
    copilot_agents/) to regenerate references/builder-code.md, then
    re-zip. A stale builder-code.md gives HASH MISMATCH in Copilot.
  * Large documents: the agent writes cases in parts (tc_part_NN.py in
    its sandbox, merged by assemble()); a workbook with any requirement
    lacking test cases is refused unless that requirement carries
    "dropped": "Q-nn" (every test waits on an unanswered question) or
    "not_testable": "<reason>".
  * Left-out tests: each clarification may carry "dropped": [test
    titles] (shown on the Clarifications sheet) and, once answered,
    "answer": "..." (status Answered; "dropped" must then be empty).
  * The generic-wording list is GENERIC in scripts/tc_builder.py (with
    OPEN for "To be confirmed" / assumption wording); keep
    quality-standard.md section 3 in step with it.
  * After changing the skill folder, zip it again with SKILL.md at the
    top level of the zip and only one folder level below it
    (references/, scripts/) - Agent Builder rejects deeper nesting.
  * Theme colours come from AppAssist email_theme.py; if the AppAssist
    palette changes, update the colour constants at the top of
    tc_builder.py.

TESTED
------------------------------------------------------------------------
  2026-11-24, locally (not yet inside Copilot): the sample built from
  the unzipped skill opens in Excel with all six sheets, 33 test cases,
  100% requirement coverage and no formula errors; changing Status
  values updates the dashboard and RTM; a spec with deliberate mistakes
  is refused with one ERROR per mistake.
  2026-11-25, locally: sample rebuilt under the "left out, never To be
  confirmed" rule (32 cases, 6 questions, 10 tests left out); 0 formula
  errors in Excel; a requirement with all tests dropped shows "Dropped"
  in the RTM and counts in "Not Covered / Dropped"; the generic output
  users reported (Verify requirement for FR_REQ_01, valid role, open
  related screen, requirement base data, behaves as per specification)
  is refused by build_workbook itself; builder-code.md installs from
  the zip with HASH OK + SELFTEST OK.
