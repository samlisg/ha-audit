# HA Audit — οδηγός εγκατάστασης (Ελληνικά)

> 🤖 **Vibe coded:** φτιάχτηκε με τη βοήθεια AI (Claude). Έχει αυτοματοποιημένα tests, αλλά δεν έχει δοκιμαστεί
> σε πολλά setups — έλεγξε τον κώδικα και κάθε εύρημα πριν αφαιρέσεις οτιδήποτε.


## 1. Εγκατάσταση στο Home Assistant
**Με HACS:**
1. HACS → ⋮ (πάνω δεξιά) → **Custom repositories**.
2. Repository: `https://github.com/samlisg/ha-audit` · Category: **Integration** → Add.
3. Βρες **HA Audit** → Download → **Restart Home Assistant**.

**Χωρίς HACS (π.χ. private repo):** αντίγραψε τον φάκελο `custom_components/ha_audit`
στο `/config/custom_components/ha_audit/` (Samba, SSH ή File editor add-on) και κάνε restart.

## 2. Ρύθμιση
1. **Settings → Devices & services → Add integration → HA Audit**.
2. Ρυθμίσεις (αλλάζουν αργότερα από **Configure**):
   * αυτόματο τρέξιμο κάθε N ημέρες (0 = μόνο χειροκίνητα, προεπιλογή 7)
   * σήμανση automations/entities χωρίς δραστηριότητα για N ημέρες (30)
   * σήμανση entities unavailable για N ημέρες (3)
   * ανάλυση βάσης recorder, diagnostic entities, ειδοποίηση για νέα ευρήματα
3. Στο sidebar εμφανίζεται το **HA Audit** (μόνο για admin χρήστες). Πάτα **Run now**.
   Αν δεν το δεις, κάνε hard refresh (Ctrl+F5 / στην εφαρμογή: Settings → Companion app → Reset frontend cache).

## 3. Χρήση
* Τα πλακίδια πάνω είναι κατηγορίες· πάτημα ανοίγει τη λίστα. Το πεδίο φίλτρου ψάχνει σε όλα.
* Κόκκινο = πιθανώς σπασμένο (αναφορές σε entities που δεν υπάρχουν, νεκρά devices…),
  πορτοκαλί = για έλεγχο/καθάρισμα. Το **NEW** δείχνει ό,τι εμφανίστηκε από την προηγούμενη φορά.
* Sensors: `sensor.ha_audit_findings`, `sensor.ha_audit_new_findings`, `sensor.ha_audit_last_run`.
* Service: `ha_audit.run` · Event: `ha_audit_completed`.

Push στο κινητό μόνο όταν υπάρχουν νέα ευρήματα: δες το automation στο README.

## 4. False positives
Φτιάξε `/config/ha_audit_ignore.yaml` (δες README). Παράδειγμα:
```yaml
entities: [sensor.node_red_*]
platforms: [zha]
hacs: ["*card-mod*"]
```

## 5. Αντιμετώπιση προβλημάτων
| Πρόβλημα | Λύση |
|---|---|
| Δεν εμφανίζεται το panel | Restart HA, hard refresh, δες ότι ο χρήστης είναι admin |
| Το panel είναι στο `/ha-audit-report` και όχι στο `/ha-audit` | Υπάρχει ήδη dashboard με URL `ha-audit` (π.χ. από την παλιά έκδοση). Διέγραψέ το από **Settings → Dashboards** και κάνε reload το integration αν θες το σύντομο URL. Το dashboard σου δεν πειράζεται ποτέ |
| Ευρήματα «Files that failed to parse» | Σπασμένο YAML/JSON στο config σου ή αποτυχία ενός check — δες το μήνυμα, δεν σταματά το υπόλοιπο audit |
| «No report yet» | Πάτα **Run now** ή τρέξε το service `ha_audit.run` |
| Λίγα/πολλά ευρήματα | Άλλαξε `stale_days` / `unavail_days` από το Configure |
| Σφάλμα στο Run | Settings → System → Logs, ψάξε `ha_audit` |
| Θες να σταματήσει | Settings → Devices & services → HA Audit → ⋮ → Delete |

## 6. Ασφάλεια
* Τίποτα δεν γράφεται στο `www/`. Το report μένει στο `/config/.storage/ha_audit`.
* Το report φτάνει στο UI μόνο μέσω authenticated WebSocket και μόνο για admins.
* Το audit είναι **μόνο ανάγνωση** — δεν διαγράφει και δεν αλλάζει τίποτα.
