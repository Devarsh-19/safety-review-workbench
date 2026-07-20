# AstroTalk Safety Review Workbench — Approach Notes

## 1. Project Overview & Methodology
The AstroTalk Safety Review Workbench is designed to identify, classify, and facilitate human review of policy-violating content in consultant-user sessions across both text chat and audio calls. 

The methodology uses a multi-layered approach:
1. **Pre-processing (Data Loader/Chunking)**: Raw sessions are ingested, parsed into turns/segments, and (for chat) grouped by consultant across sessions.
2. **LLM Inference Engine**: We leverage Google Gemini 3 Flash Preview fffor high-throughput, context-aware semantic analysis of sessions. The LLM identifies violations based on an exhaustive intent taxonomy.
3. **Aggregation & Rule Engine**: Model outputs are mapped into a standardized set of canonical flags and subjected to escalation rules (e.g., combination of specific flags triggering a severe alert).
4. **Human-in-the-Loop Review**: All flagged sessions are passed to a dual-tier (L1/L2) human review queue via the React/FastAPI workbench interface for validation, correction, and final enforcement lock. Clean sessions bypass human review entirely, greatly reducing operational overhead.

## 2. Intent Taxonomy & Severity Mapping
The intent taxonomy defines 22 specific violations, mapped to internal database states. The LLM classifies each identified violation using the exact intent strings.

### RED Severity (Immediate Action Required)
These intents result in an immediate `SEVERE` session verdict:
- `NSFW`: Sexually inappropriate content (general)
- `NSFW_EXPLICIT`: Explicit sexual acts/descriptions in consultation
- `NSFW_GROOMING`: Romantic solicitation, grooming pattern across messages
- `NSFW_APPEARANCE`: Inappropriate questions about appearance or body
- `CSAM_RISK`: Content involving minors sexually (Highest priority; >0.2 confidence threshold)
- `FINANCIAL_SOLICITATION`: Asking for money outside AstroTalk payment system
- `IDENTITY_FRAUD`: Impersonation or requesting sensitive ID/bank info
- `ABUSIVE_LANGUAGE`: Vulgar, profane, or disrespectful language
- `HATE_SPEECH`: Hatred based on religion, caste, gender, community
- `FAKE_REMEDIES`: Promising guaranteed results or dangerous/illegal remedies
- `UNAUTHORIZED_MEDICAL_ADVICE`: Medical diagnosis or advising against doctors
- `SELF_HARM`: Encouraging or advising on self-harm/suicide
- `VIOLENCE`: Promoting or encouraging violent acts
- `INSTIGATION`: Inciting fights, arguments, or harmful actions

### AMBER Severity (Warning, Needs Review)
These intents result in a `FLAGGED` session verdict. However, certain combinations (e.g., Off-Platform + Personal Data) will escalate the session to `SEVERE`.
- `OFF_PLATFORM_SOLICITATION`: Moving conversation to WhatsApp/Telegram/phone
- `PERSONAL_DATA_COLLECTION`: Asking for Aadhaar, bank details, passwords
- `FEAR_MANIPULATION`: Doom predictions to pressure into paid remedies
- `COMPETITOR_PROMOTION`: Promoting other astrologers, apps, websites
- `EXTERNAL_MEDIA_CONTENT`: External links/media sharing (detected via regex layer)
- `OTHER`: Any policy violation not covered above

## 3. Escalation Rules
A session's overall verdict is determined programmatically based on its active flags:
- If **ANY** Red-severity flag is present: Verdict is `SEVERE`.
- If **NO** Red flags are present, but **ANY** Amber-severity flag is present: Verdict is `FLAGGED`.
- **Exception Combinations**: If specific Amber flags co-occur, they escalate the session to `SEVERE`.
  - Off-Platform + Personal Data
  - Off-Platform + Fear Manipulation
  - Personal Data + Fear Manipulation
  - External Media + Personal Data

## 4. Multilingual & Slang Handling
Given AstroTalk's demographic, sessions occur in a mix of English, Hindi, Hinglish, and various regional languages (Tamil, Telugu, Marathi, Punjabi, Bengali, Kannada, Malayalam, Gujarati).
- The LLM acts as the primary multilingual translation and detection engine, instructed to "mentally translate" regional text/audio to English while preserving cultural context.
- **Slang Dictionaries**: The system prompt contains explicit regional slang mappings for abusive language (e.g., specific profanity in Tamil, Telugu, Marathi, etc.) to ensure the LLM captures localized hostility and vulgarity without missing context.
- **Cultural Nuance**: The instructions explicitly differentiate between professional astrological terms, cultural terms of endearment ("beta", "ji"), tarot warmth ("hun", "babe"), and actual grooming or inappropriate behavior.

## 5. Audio Pipeline Specifics
- Audio is processed through a similar pipeline but at the **segment** level rather than the **turn** level.
- The pipeline consumes JSON files containing diarized speaker segments (e.g., `SPEAKER_1`, `SPEAKER_2`), transcripts, and tone analysis.
- **Long Pauses**: Pauses in audio are detected locally, with the review threshold set to flag sessions containing pauses of **60 seconds or more**.
- The reviewer assigns actual roles (`ASTROLOGER`, `USER`) during the L1 manual review via the Audio Session Viewer interface.

## 6. Assumptions & Quality Controls
- **Automated Messages**: Chat turns flagged as `is_automated_message=1` in the source data are completely skipped during LLM inference to save tokens and prevent false positives from platform templated messages.
- **Confidence Thresholds**: Flags with LLM confidence below 0.5 are discarded (except `CSAM_RISK` which uses an aggressive 0.2 threshold).
- **False Positive Mitigation**: `OTHER` is heavily penalized in the prompt to prevent the LLM from using it as a catch-all for borderline behavior. The system explicitly instructs the LLM not to flag short conversational fillers ("hmm", "ok", "Y") unless they match known harmful abbreviations.
- **Amendment System**: The database supports "soft-deletes" (Dismissal) and flag amendments. If a reviewer changes a flag's category, a new row is created with a `parent_flag_id` pointing to the original, preserving full audit history.
