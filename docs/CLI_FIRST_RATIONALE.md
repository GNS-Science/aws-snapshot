# CLI-First Design Rationale

## Decision: Python/Click CLI over Web Application

**Date:** 2026-03-09  
**Status:** Approved  
**User Profile:** Command-line boffins (technical DevOps/Engineering team)

---

## Executive Summary

The NSHM backup solution will be implemented as a **CLI-first tool** using Python and Click. This decision aligns with the technical proficiency of the user group, reduces maintenance overhead, and provides better integration with existing DevOps workflows.

A simple dashboard can be added later if needed (e.g., Streamlit app, static HTML reports).

---

## CLI Approach - Advantages

### Technical Benefits

| Benefit | Description | Impact |
|---------|-------------|--------|
| **Scriptable** | Commands can be automated in CI/CD, cron, EventBridge | High |
| **SSH-friendly** | Works on bastion hosts without browser access | High |
| **JSON output** | Pipe to `jq`, grep, or other tools (`backup status --output json`) | Medium |
| **Version-controlled config** | YAML/JSON files in git alongside code | Medium |
| **Audit trail** | Command history in shell logs (`.bash_history`, CloudWatch) | High |
| **Lower maintenance** | No frontend framework, dependencies, or security patches | High |
| **Lambda-native** | Same Python runtime, no API Gateway complexity | High |
| **Dry-run culture** | `--dry-run` flag on all mutating operations | Medium |
| **Power user speed** | Muscle memory, tab completion, aliases | Medium |

### Cost Benefits

| Factor | CLI | Web App | Savings |
|--------|-----|---------|---------|
| Hosting | Lambda only | Lambda + EC2/Amplify/API Gateway | ~$50-100/month |
| Development time | 8-10 weeks | 12-16 weeks | 4-6 weeks dev effort |
| Ongoing maintenance | Python deps only | Frontend + backend security updates | ~4 hours/month |
| Authentication | IAM roles | Cognito/SSO integration | ~2 weeks initial setup |

---

## Web App Approach - Why Not

### When a Web App Would Be Better

A web application would be justified if:
- ❌ Non-technical stakeholders need frequent access
- ❌ Real-time collaboration features are required
- ❌ Visual data exploration is a primary use case
- ❌ Customer-facing product (not internal tool)
- ❌ 10+ concurrent users regularly

**None of these apply to NSHM backup tool.**

### Web App Disadvantages (for this use case)

| Disadvantage | Impact |
|--------------|--------|
| Requires hosting (EC2, Amplify, or separate service) | Additional cost, complexity |
| Authentication complexity (Cognito, SSO integration) | 2+ weeks setup, ongoing maintenance |
| Not scriptable (need separate API for automation) | Duplicates effort |
| Browser dependency (no SSH-only workflows) | Limits ops flexibility |
| Frontend maintenance debt (framework updates, security patches) | Ongoing time cost |
| Overkill for 3-5 power users | Poor ROI |

---

## User Profile Analysis

### NSHM Team Characteristics

| Characteristic | Assessment |
|----------------|------------|
| Technical proficiency | High (DevOps/Engineering) |
| Preferred interface | Terminal/SSH |
| Automation needs | High (scheduled jobs, CI/CD) |
| Number of users | 3-5 power users |
| Frequency of use | Daily/weekly operations |
| Stakeholder visibility | Monthly reports sufficient |

**Conclusion:** CLI is the natural fit for this user group.

---

## Hybrid Option: CLI + Lightweight Dashboard

If visual status updates are needed for stakeholders:

### Option 1: Static HTML Reports
```bash
$ backup report --format html --output s3://nsdm-backup-reports/
```
- Generated on each backup run
- Hosted on S3 + CloudFront (~$1-2/month)
- Simple, no maintenance
- Read-only, no interactivity

### Option 2: Streamlit Dashboard (Future)
- Separate `streamlit run` app (can be in same repo)
- Reads same config/logs as CLI
- 2-3 days development effort
- Deploy on ECS Fargate or local workstation
- **Defer until needed**

### Option 3: Slack Integration (Already Planned)
- Real-time notifications in `#nsdm-backups` channel
- Green/red status indicators
- Click-through links to CloudWatch logs
- No additional infrastructure

---

## CLI Command Design Principles

To maximize CLI effectiveness:

1. **Consistent structure:** `backup <resource> <action> [options]`
2. **Sensible defaults:** Minimize required flags
3. **Dry-run everywhere:** All mutating operations support `--dry-run`
4. **Multiple output formats:** `--output text|json|yaml`
5. **Verbose mode:** `--verbose` for debugging
6. **Help everywhere:** `--help` on every command/subcommand
7. **Exit codes:** 0=success, 1=error, 2=dry-run (for scripting)
8. **Progress indicators:** Spinners/bars for long operations

---

## Comparison Matrix

| Feature | CLI | Web App | Winner |
|---------|-----|---------|--------|
| **Learning curve** | Steep (technical users) | Shallow (anyone) | Context-dependent |
| **Automation** | Native (shell scripts) | Requires API | ✅ CLI |
| **Real-time updates** | Manual re-run | WebSockets/polling | ✅ Web App |
| **Visual status** | Text-based | Charts/dashboards | ✅ Web App |
| **Scriptable** | Yes | No (need API) | ✅ CLI |
| **SSH/bastion** | Full support | Browser required | ✅ CLI |
| **Audit trail** | Shell history, logs | App logging required | ✅ CLI |
| **Hosting cost** | Lambda only | Lambda + frontend | ✅ CLI |
| **Maintenance** | Python deps only | Full stack | ✅ CLI |
| **Onboarding** | Documentation + practice | Intuitive UI | ✅ Web App |
| **Power user speed** | Fast (muscle memory) | Slower (navigation) | ✅ CLI |

---

## Future Considerations

### Triggers to Reconsider Web App

Revisit this decision if:
- Team grows to 10+ regular users
- Non-technical stakeholders demand frequent access
- Management requires real-time executive dashboard
- Compliance requires auditable UI-based workflows

### Easy Migration Path

If web UI becomes necessary:
1. CLI remains the primary interface (no deprecation)
2. Build **internal API** that CLI uses
3. Add lightweight frontend (Streamlit, React) consuming same API
4. No breaking changes, no migration needed

---

## Risks & Mitigations

| Risk | Probability | Mitigation |
|------|-------------|------------|
| New team members struggle with CLI | Low | Documentation, examples, training session |
| Stakeholders want visual dashboard | Medium | Generate HTML reports, Slack status |
| CLI feels "unprofessional" to leadership | Low | Focus on cost savings, reliability metrics |
| Hard to see "big picture" status | Low | `backup status` dashboard command, Slack summaries |

---

## Success Metrics

CLI-first approach is successful if:
- ✅ 90%+ of backups initiated without manual intervention
- ✅ Team can execute restore within 5 minutes (no training required)
- ✅ Zero requests for "easier interface" after 3 months
- ✅ Slack notifications satisfy stakeholder visibility needs
- ✅ Monthly HTML reports satisfy compliance requirements

---

## Recommendation

**Proceed with CLI-first implementation.**

This decision is based on:
1. **User profile:** Technical team prefers and expects CLI
2. **Cost:** Lower development and hosting costs
3. **Fit:** Better integration with DevOps workflows
4. **Flexibility:** Can add lightweight dashboard later if needed
5. **Speed:** Faster to market (8-10 weeks vs 12-16 weeks)

**Revisit decision after 6 months** if stakeholder feedback indicates need for visual interface.

---

## Alternatives Considered

| Alternative | Description | Why Rejected |
|-------------|-------------|--------------|
| **Full web app (React + API)** | Complete dashboard with authentication | Overkill for 3-5 power users, high maintenance |
| **Streamlit from day 1** | Python-based dashboard alongside CLI | Adds 2-3 weeks dev time, not needed yet |
| **AWS Console-based** | Native AWS Console + Custom UI via Console Extensions | Limited customization, AWS vendor lock-in |
| **Both CLI + Web App** | Build both interfaces simultaneously | Doubles development effort, poor ROI |
| **CLI now, Web App later** | Phased approach (current recommendation) | Delays launch, but defers cost until validated |

---

**Document Version:** 1.0  
**Created:** 2026-03-09  
**Status:** Approved  
**Owner:** NSHM DevOps Team  
