"""Idempotent seed data: Stanford Golf Course + holes that match the
Unity MapController's `courseHoles` array.

Source of truth: GolfHUD/Assets/Scenes/SampleScene.unity (HoleProfile entries
on the MapController). When you add/edit holes in the Unity Inspector,
update this file too.
"""

from sqlalchemy.orm import Session

from models import Course, Hole

STANFORD_COURSE = {
    "course_id": 1,
    "name": "Stanford Golf Course",
    "location": "Stanford, CA",
}

# (number, par, posted_yards) — yardage is null until measured / posted.
STANFORD_HOLES = [
    (1, 5, None),
    (2, 4, None),
]


def seed(db: Session) -> None:
    course = db.query(Course).filter(Course.course_id == STANFORD_COURSE["course_id"]).first()
    if course is None:
        course = Course(**STANFORD_COURSE)
        db.add(course)
        db.flush()

    for number, par, yardage in STANFORD_HOLES:
        # hole_id == number for the single-course case → keeps Unity's
        # opaque hole_id integers aligned with the DB primary key.
        existing = db.query(Hole).filter(Hole.hole_id == number).first()
        if existing is None:
            db.add(
                Hole(
                    hole_id=number,
                    course_id=course.course_id,
                    number=number,
                    par=par,
                    yardage=yardage,
                )
            )

    db.commit()
