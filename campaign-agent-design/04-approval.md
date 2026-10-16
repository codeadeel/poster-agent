# approval

One card, and screens and email are approved separately, because an email can't be unsent.

```mermaid
sequenceDiagram
    participant Ag as Agent
    participant Ap as Approval service
    participant H as Person
    participant P as Platform
    Ag->>Ap: request_approval(drafts)
    Ap->>H: card
    Note over H: poster previews<br/>screens + schedule in local time (Thu 15 Oct to Sat 17 Oct, 10:00 to 16:00)<br/>email preview + subject + recipient count + send time<br/>assumptions, warnings, cost
    H->>Ap: approve screens / approve email / request changes
    Ap->>P: publish_schedule / send_campaign (token tied to hash)
    P-->>Ap: receipt
    Ap-->>Ag: result with receipts
```

The card also has a "send me a test" button, run by the platform. An unusually large audience needs a second confirmation. Only people with publish permission can approve.

[back to the overview](README.md)
