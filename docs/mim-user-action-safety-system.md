# MIM User Action Safety Monitoring and Harm Prevention

## Overview

MIM now monitors user actions for potential harm to system integrity and automatically inquires about user intentions before allowing dangerous operations. This creates a protective layer that prevents unintended damage while maintaining transparency and auditability.

## Key Capabilities

- **Automatic Harm Detection** - Identifies potentially dangerous user actions
- **Risk Assessment** - Classifies actions by risk level (safe → critical)
- **Intent Inquiry** - Asks clarifying questions about user motivations
- **Confirmation Workflow** - Requires explicit intent confirmation for high-risk actions
- **Full Audit Trail** - Records all assessments, inquiries, and decisions
- **Operator Control** - Empowers operators to make final approval decisions

## Architecture

### Components

1. **User Action Safety Monitor** (`core/user_action_safety_monitor.py`)
   - Monitors user actions for potential harm
   - Classifies actions into categories
   - Assesses risk level based on predefined rules
   - Suggests mitigations for dangerous operations
   - Generates inquiry prompts for risky actions

2. **User Action Inquiry Service** (`core/user_action_inquiry_service.py`)
   - Creates structured inquiries from risk assessments
   - Collects user responses to safety questions
   - Manages inquiry lifecycle and audit trail
   - Enables operator approval/rejection workflow

3. **Safety Router** (`core/routers/safety_router.py`)
   - REST API for action assessment
   - Inquiry creation and response endpoints
   - Pendency tracking (who's awaiting response, who's awaiting approval)
   - Health check aggregation

## Action Categories and Risk Levels

### Categories Monitored

| Category | Examples | Risk | Action |
|----------|----------|------|--------|
| **Software Installation** | Installing packages, runtime updates | HIGH | Inquiry required |
| **System Core Modification** | Kernel changes, core service modification | CRITICAL | Inquiry + approval |
| **Permission Changes** | User/role modifications, access control | HIGH | Inquiry required |
| **Data Deletion** | Database wipeouts, config removal | HIGH | Inquiry required |
| **Configuration Changes** | Tuning parameters, switching backends | MEDIUM | Inquiry required |
| **Network Modification** | Routing changes, firewall rules | HIGH | Inquiry required |
| **Security Rule Changes** | Disabling auth, removing restrictions | CRITICAL | Inquiry + approval |
| **Service Control** | Stopping critical services | MEDIUM | Inquiry required |
| **Resource Limit Changes** | CPU/memory caps, rate limiting | MEDIUM | Inquiry required |

### Risk Levels

```
SAFE
  ↓ (Generally harmless, automatic execution OK)
LOW
  ↓ (Minor impact, warning sufficient)
MEDIUM
  ↓ → Inquiry Required
HIGH
  ↓ → Inquiry + Intent Confirmation
CRITICAL
  ↓ → Inquiry + Full Approval Workflow
```

## Safety Inquiry Workflow

### For High-Risk Actions

```
User Initiates Action
    ↓
MIM Assesses Risk
    ↓
Risk Level ≥ MEDIUM?
    ├─ No → Action proceeds (with logging)
    └─ Yes → Create Safety Inquiry
        ↓
User Receives Inquiry with:
- Risk level explanation
- Specific concerns
- Clarifying questions
- Required mitigations
        ↓
User Responds:
- Answers to specific questions
- Statement of intent/understanding
- Acknowledgment of risks
        ↓
Response Evaluated by Operator/System
        ├─ Approved → Action proceeds
        └─ Rejected → Action blocked (reason recorded)
        ↓
Audit Trail Recorded:
- Assessment
- Inquiry questions
- User responses
- Approval decision
- Reasoning
```

### Inquiry Lifecycle

1. **CREATED** - Inquiry generated from risk assessment
2. **AWAITING_RESPONSE** - User should respond with answers
3. **RESPONSE_RECEIVED** - User submitted answers, awaiting approval
4. **ACTION_APPROVED** - Approved, action can proceed
5. **ACTION_REJECTED** - Rejected, action blocked
6. **EXPIRED** - Inquiry timed out without response

## Concerns and Questions

MIM automatically generates safety questions tailored to each action category:

### Software Installation
- What is the purpose of installing this software?
- Have you verified this software is compatible with MIM?
- Do you understand the security implications?
- Is this installation necessary for MIM to function?

### System Core Modification
- Are you explicitly modifying OS core components?
- Do you have approval from system administrators?
- What is the specific change you are making?
- Have you backed up critical system state?
- Who is responsible for system recovery if something fails?

### Security Rule Changes
- What security rules are being modified?
- Why are these rules being weakened?
- Who authorized this security change?
- What compensating controls will be in place?
- How will this be audited and monitored?

### Data Deletion
- What data will be deleted?
- Have you verified this data is not needed for compliance?
- Is there a backup of this data?
- What is the business justification for deletion?

### Permission Changes
- What permissions are being changed and why?
- Who will gain access as a result?
- Are these permission changes compliant with security policy?
- Have you considered least-privilege principles?

## Mitigations Suggested

### For Each Category:

**Software Installation:**
- Scan software for security vulnerabilities
- Install in isolated test environment first
- Verify all dependencies are compatible

**System Core Modification:**
- Have system administrator verify change
- Prepare rollback procedure before proceeding
- Monitor system health immediately after change

**Data Deletion:**
- Ensure backup exists before proceeding
- Verify deletion target is non-critical
- Log deletion action with timestamp and user

**Network Modification:**
- Test network connectivity after change
- Maintain alternative communication channels
- Have emergency isolation plan

**Security Changes:**
- Document all changes comprehensively
- Establish compensating controls
- Schedule security audit after change
- Have rollback plan ready

## API Endpoints

### Action Assessment

```
POST /mim/safety/assess-action
Request: {
  "user_id": "admin@example.com",
  "action_type": "command_execution",
  "description": "Installing python-requests via pip",
  "category": "software_installation",
  "command": "pip install requests"
}
Response: {
  "action_id": "action-abc123",
  "risk_level": "high",
  "risk_category": "software_installation",
  "reasoning": "Action classified as software_installation",
  "specific_concerns": [
    "May introduce security vulnerabilities",
    "Could alter system behavior unpredictably",
    ...
  ],
  "safe_to_execute": false,
  "recommended_inquiry": true
}

GET /mim/safety/assess-action/{action_id}
  → Get previous assessment
```

### Inquiry Management

```
POST /mim/safety/inquiries?action_id=abc&user_id=admin@example.com
  → Create inquiry

GET /mim/safety/inquiries
  → List all inquiries (can filter by status)

GET /mim/safety/inquiries/{inquiry_id}
  → Get inquiry details

GET /mim/safety/inquiries/{inquiry_id}/prompt
  → Get human-readable prompt for display

POST /mim/safety/inquiries/{inquiry_id}/respond
Request: {
  "answers": {
    "question_1": "Installing to update dependencies",
    "question_2": "Yes, verified with requirements.txt",
    ...
  },
  "understanding": "I understand this may affect system behavior..."
}
  → Submit responses

POST /mim/safety/inquiries/{inquiry_id}/approve
Request: { "approved": true, "reason": "Verified safe version" }
  → Approve or reject inquiry response

GET /mim/safety/inquiries/pending/user/{user_id}
  → Get user's pending inquiries

GET /mim/safety/inquiries/pending/approval
  → Get inquiries awaiting operator approval
```

## Integration with Autonomy Lifecycle

The user action safety system integrates with MIM's autonomy lifecycle (objectives 39-45):

1. **Objective 39** - Foundation: Safety rules and assessment logic
2. **Objective 40** - Planning & Execution: Check for harmful actions in decision loop
3. **Objective 41** - Learning: Track which user actions typically cause issues
4. **Objective 42** - Communication: Notify operators of safety concerns
5. **Objective 43** - Governance: Full audit trail of approvals
6. **Objective 44** - Adaptation: Refine harm detection based on feedback
7. **Objective 45** - Resilience: Prevent cascading failures from bad changes

## Dangerous Action Examples

### Examples Requiring Inquiry

**Software Installation:**
```bash
pip install mysterious-package-from-untrusted-repo
```
→ Triggers HIGH risk inquiry about source verification

**System Core Modification:**
```bash
sudo vim /boot/grub/grub.cfg
```
→ Triggers CRITICAL risk inquiry with rollback requirements

**Security Rule Removal:**
```bash
iptables -F  # Flush all firewall rules
```
→ Triggers CRITICAL risk inquiry about compensating controls

**Data Deletion:**
```bash
rm -rf /var/lib/mim/data/*
```
→ Triggers HIGH risk inquiry about backups and business justification

**Permission Escalation:**
```bash
usermod -G sudo unprivileged_user
```
→ Triggers HIGH risk inquiry about need and policy compliance

### Examples That Are Safe

**Everyday Operations:**
- Viewing logs
- Creating working directories
- Running tests
- Generating reports

**Standard Maintenance:**
- Restarting services (if monitored)
- Clearing temporary files
- Rotating logs
- Updating documentation

## Testing and Validation

### Manual Testing

```bash
# Assess a software installation action
curl -X POST http://127.0.0.1:18001/mim/safety/assess-action \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "admin@example.com",
    "action_type": "command",
    "description": "Installing python package",
    "category": "software_installation",
    "command": "pip install requests"
  }' | jq .

# Create inquiry
curl -X POST http://127.0.0.1:18001/mim/safety/inquiries?action_id=<ACTION_ID>&user_id=admin@example.com \
  -H "Content-Type: application/json" \
  -d '{"action_description": "Installing python-requests"}' | jq .

# View inquiry prompt
curl http://127.0.0.1:18001/mim/safety/inquiries/<INQUIRY_ID>/prompt | jq .

# Submit response
curl -X POST http://127.0.0.1:18001/mim/safety/inquiries/<INQUIRY_ID>/respond \
  -H "Content-Type: application/json" \
  -d '{
    "answers": {
      "question_1": "Updating dependencies for security",
      "question_2": "Yes, checked requirements"
    },
    "understanding": "Understood risks around package compatibility"
  }' | jq .

# Operator approval
curl -X POST http://127.0.0.1:18001/mim/safety/inquiries/<INQUIRY_ID>/approve \
  -H "Content-Type: application/json" \
  -d '{"approved": true, "reason": "Verified safe package version"}' | jq .
```

### Programmatic Integration

```python
import httpx
import asyncio

async def check_action_safety():
    async with httpx.AsyncClient() as client:
        # Assess action
        assessment = await client.post(
            "http://127.0.0.1:18001/mim/safety/assess-action",
            json={
                "user_id": "admin",
                "action_type": "command",
                "description": "Deleting old logs",
                "category": "data_deletion",
                "command": "rm -rf /var/log/old/*"
            }
        )
        
        action_id = assessment.json()["action_id"]
        print(f"Risk level: {assessment.json()['risk_level']}")
        
        if assessment.json()["recommended_inquiry"]:
            # Create inquiry
            inquiry = await client.post(
                f"http://127.0.0.1:18001/mim/safety/inquiries"
                f"?action_id={action_id}&user_id=admin",
                json={"action_description": "Deleting old logs"}
            )
            
            inquiry_id = inquiry.json()["inquiry_id"]
            
            # Get prompt
            prompt = await client.get(
                f"http://127.0.0.1:18001/mim/safety/inquiries/{inquiry_id}/prompt"
            )
            print(prompt.json()["prompt"])

asyncio.run(check_action_safety())
```

## Governance Principles

1. **Transparency** - All safety assessments and decisions are logged
2. **No Silent Failures** - High-risk actions are never silently blocked
3. **Operator Authority** - Operators make final approval decisions
4. **Auditability** - Complete chain of reasoning preserved
5. **Remediation Path** - Users always know how to proceed after rejection
6. **Education** - Inquiry process helps users understand risks

## Future Enhancements

1. **Machine Learning** - Learn which types of actions typically cause issues
2. **Behavioral Analysis** - Detect anomalous user action patterns
3. **Reputation Scoring** - Track user history of safe/unsafe actions
4. **Collaborative Approval** - Require multiple operator approvals for critical changes
5. **Automated Rollback** - Auto-recover if monitored metrics degrade after action
6. **Integration with Constraints** - Use autonomy constraints to guide safety rules
7. **Proactive Warnings** - Predict harmful outcomes before they occur

## Key Principles

- **MIM never blocks without explanation**
- **All decisions are auditable and reversible**
- **Users understand why actions are questioned**
- **Operators retain final authority**
- **Safety doesn't prevent legitimate action**
- **Intent matters - malformed questions get rejected**

## Contact Integration Points

- **Orchestration Service** - Check action safety in main decision loop
- **Operator Dashboard** - Display pending inquiries and approval decisions
- **Audit System** - Record all assessments and approvals
- **Constraint Engine** - Use safety rules to inform constraints
- **Decision Records** - Log approved/rejected actions as decisions
