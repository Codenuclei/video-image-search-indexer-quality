"""Unit tests for Masters' Union leadership scrape helpers (no DB)."""

from __future__ import annotations

from app.reid.mu_leadership import parse_leadership_cards


SAMPLE = """
<div class="masterCardBoxi">
  <img src="https://cdn.example.com/pratham.jpg" alt="Pratham" />
  <img src="https://cdn.example.com/linkedin.svg" alt="li" />
  <div class="masterName">Pratham Mittal</div>
  <div class="designationOfMaster">Founder</div>
  <a href="https://www.linkedin.com/in/pratham">LI</a>
</div>
<div class="masterCardBoxi">
  <img src="/images/swati.png" alt="Swati" />
  <div class="masterName">Swati Ganeti</div>
  <div class="designationOfMaster">CEO</div>
</div>
"""


def test_parse_leadership_cards_extracts_portraits_and_names():
    people = parse_leadership_cards(SAMPLE, base_url="https://mastersunion.org/about-us")
    assert len(people) == 2
    assert people[0]["name"] == "Pratham Mittal"
    assert people[0]["role"] == "Founder"
    assert people[0]["image_url"] == "https://cdn.example.com/pratham.jpg"
    assert "linkedin.com/in/pratham" in people[0]["linkedin_url"]
    assert people[1]["name"] == "Swati Ganeti"
    assert people[1]["image_url"].endswith("/images/swati.png")
    assert people[1]["linkedin_url"] == ""
