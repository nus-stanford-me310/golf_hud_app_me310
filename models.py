from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    DateTime,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from datetime import datetime

from database import Base


class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    name = Column(String, nullable=False)
    handicap = Column(Float, nullable=True)
    skill_level = Column(String, nullable=True)  # beginner/intermediate/advanced/pro
    created_at = Column(DateTime, default=datetime.utcnow)

    club_distances = relationship(
        "UserClubDistance", back_populates="user", cascade="all, delete-orphan"
    )
    games = relationship("Game", back_populates="user")


class UserClubDistance(Base):
    __tablename__ = "user_club_distances"
    __table_args__ = (UniqueConstraint("user_id", "club", name="uq_user_club"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    club = Column(String, nullable=False)
    avg_yardage = Column(Float, nullable=False)

    user = relationship("User", back_populates="club_distances")


class Course(Base):
    __tablename__ = "courses"

    course_id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    location = Column(String, nullable=True)

    holes = relationship("Hole", back_populates="course", cascade="all, delete-orphan")


class Hole(Base):
    __tablename__ = "holes"

    hole_id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.course_id"), nullable=False)
    number = Column(Integer, nullable=False)
    yardage = Column(Integer, nullable=True)
    par = Column(Integer, nullable=True)
    hazard_map = Column(String, nullable=True)

    course = relationship("Course", back_populates="holes")
    games = relationship("Game", back_populates="hole")


class Game(Base):
    __tablename__ = "games"

    game_id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    hole_id = Column(Integer, ForeignKey("holes.hole_id"), nullable=True)
    start_time = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default="active")

    user = relationship("User", back_populates="games")
    hole = relationship("Hole", back_populates="games")
    shots = relationship("Shot", back_populates="game", cascade="all, delete-orphan")


class Weather(Base):
    __tablename__ = "weather"

    weather_id = Column(Integer, primary_key=True, index=True)
    temp = Column(Float, nullable=True)
    wind_speed = Column(Float, nullable=True)
    wind_dir = Column(String, nullable=True)


class Shot(Base):
    __tablename__ = "shots"

    shot_id = Column(Integer, primary_key=True, index=True)
    game_id = Column(Integer, ForeignKey("games.game_id"), nullable=False)
    hole_id = Column(Integer, ForeignKey("holes.hole_id"), nullable=True)
    weather_id = Column(Integer, ForeignKey("weather.weather_id"), nullable=True)
    shot_no = Column(Integer, nullable=False)
    gps_loc = Column(String, nullable=True)
    distance = Column(Float, nullable=True)
    ball_traj = Column(String, nullable=True)

    game = relationship("Game", back_populates="shots")
    recommendation = relationship(
        "ClubRecommendation",
        back_populates="shot",
        uselist=False,
        cascade="all, delete-orphan",
    )


class ActiveUser(Base):
    """Tracks the single 'currently signed-in' user for the Unity HUD to read.
    Single-row table — id is always 1.
    """

    __tablename__ = "active_user"

    id = Column(Integer, primary_key=True, default=1)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")


class ClubRecommendation(Base):
    __tablename__ = "club_recommendations"

    rec_id = Column(Integer, primary_key=True, index=True)
    shot_id = Column(Integer, ForeignKey("shots.shot_id"), nullable=False)
    club = Column(String, nullable=False)
    dynamic_yardage = Column(Float, nullable=True)
    confidence = Column(Float, nullable=True)

    shot = relationship("Shot", back_populates="recommendation")
