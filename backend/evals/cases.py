"""Golden cases: inputs with known good answers. Add real (anonymised) cases from your own pipeline over time;
the more they look like your customers, the more the scorecard means.

Seed contacts used below come from backend/seed_data/leads.json.
"""

# Lead Scorer: who should be rated how (a range, because A vs B can be a judgement call).
SCORER = [
    {"id": "cdo-large-manufacturer", "email": "priya.sample@example.com", "expect_tiers": ["A", "B"]},
    {"id": "tiny-startup", "email": "sam.placeholder@example.com", "expect_tiers": ["C", "D"]},
    {"id": "intern-small-agency", "new_lead": {"first_name": "Ivy", "last_name": "Intern", "company": "Two Person Studio",
     "title": "Marketing Intern", "email": "ivy@twopersonstudio.example", "employees": 2, "industry": "Design agency"},
     "expect_tiers": ["D"]},
    {"id": "vp-data-retailer", "new_lead": {"first_name": "Rafael", "last_name": "Retail", "company": "Big Retail Group",
     "title": "VP Data & Analytics", "email": "rafael@bigretail.example", "employees": 40000, "industry": "Retail",
     "message": "We are replatforming our CRM and want help with data quality."}, "expect_tiers": ["A", "B"]},
]

# Reply Triage: real-world reply styles and the category a good SDR would pick.
REPLIES = [
    ("ooo", "I am out of the office until 14 November with limited access to email. For urgent matters contact my assistant.", "out_of_office"),
    ("unsub", "Please take me off your list.", "unsubscribe"),
    ("interested", "This is timely, we're looking at exactly this. Can you do Thursday afternoon?", "interested"),
    ("referral", "I'm not the right person, but Dana Lee (dana.lee@example.com) runs our data platform. Try her.", "referral"),
    ("wrong-person", "I left that role last year and don't work on data anymore.", "wrong_person"),
    ("not-now", "Not a priority right now. Maybe reach out again in Q3.", "not_now"),
    ("question", "How is this different from what our current SI already does for us?", "question"),
    ("objection", "We already have a partner for this and we're happy with them.", "objection"),
    ("auto-ack", "Thanks for your email. This is an automated confirmation that we received your message.", "other"),
    ("curt-yes", "sure, send me something to read", "interested"),
]

# Personalisation grader: a specific email must score clearly higher than a generic one.
GRADER = [
    {
        "id": "specific-vs-generic",
        "recipient": {"name": "Priya Sample", "title": "Chief Data Officer", "company": "Siemens"},
        "evidence": ("Brief: Siemens announced a 2027 goal to unify factory data across 120 plants. Priya spoke at "
                     "Data Summit 2026 about 'forecast accuracy as a board metric'. Case study: a manufacturer raised "
                     "forecast accuracy from 61% to 84%."),
        "specific": ("Subject: forecast accuracy as a board metric\n\nPriya, your Data Summit point that forecast "
                     "accuracy belongs on the board agenda stuck with me, especially with the goal to unify data "
                     "across 120 plants. A manufacturer we worked with went from 61% to 84% forecast accuracy once "
                     "plant data was unified. Is that the metric you're being measured on this year?"),
        "generic": ("Subject: quick question\n\nHi Priya, I hope you're well. We help companies like yours unlock "
                    "the value of their data with modern solutions. Would you be open to a 15-minute call next week "
                    "to explore synergies?"),
        "min_gap": 20,
    },
]

# Outreach Writer: a full sequence for a seeded contact with a brief. Checked by rules and a judge.
WRITER = [
    {
        "id": "priya-siemens",
        "email": "priya.sample@example.com",
        "brief": {
            "summary": "Siemens is unifying factory data across plants and investing in AI-driven forecasting.",
            "priorities": ["Unify factory data across 120 plants by 2027", "Improve demand forecast accuracy"],
            "pain_points": ["Fragmented plant data", "Forecasts not trusted by planners"],
            "recent_news": ["2026-09 - Siemens announces factory data unification programme - https://news.example.com/siemens-data"],
            "recommended_angle": "Forecast accuracy as a board-level metric, enabled by unified plant data.",
            "relevant_proof_points": [], "likely_objections": ["We have an incumbent integrator"],
            "sources": ["https://news.example.com/siemens-data"],
        },
        "steps": 3,
    },
]
