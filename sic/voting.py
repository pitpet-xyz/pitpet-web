import collections
import typing
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from django.utils.timezone import make_aware


class PostRanking(ABC):
    @abstractmethod
    def story_hotness(self, story: "sic.models.Story") -> int:
        ...

    def story_hotness_dict(
        self, story: "sic.models.Story"
    ) -> typing.Optional[typing.Dict[typing.Any, typing.Any]]:
        None


class KarmaRanking(PostRanking):
    HOTNESS_WINDOW = 60 * 60 * 22

    def story_hotness(self, story: "sic.models.Story") -> int:
        return self.story_hotness_dict(story)["score"]

    def story_hotness_dict(
        self, story: "sic.models.Story"
    ) -> typing.Optional[typing.Dict[typing.Any, typing.Any]]:
        user_id = story.user_id
        domain_penalty = 0.0
        tag_hotness = sum(map(lambda t: t.hotness_modifier(), story.tags.all()))
        score = float(story.karma) + tag_hotness - domain_penalty
        comment_score_modifier = 0
        for c in story.comments.all():
            if c.user_id == user_id:
                continue
            score += 0.25 * c.karma
            comment_score_modifier += 0.25 * c.karma
        now = make_aware(datetime.utcnow())
        age = now - story.created
        age = timedelta(days=age.days, seconds=age.seconds)
        time_window_penalty = -round(
            float(age.total_seconds()) / self.HOTNESS_WINDOW, 3
        )
        score += time_window_penalty
        return {
            "score": score,
            "time_window_penalty": time_window_penalty,
            "age": age,
            "comment_score_modifier": comment_score_modifier,
            "tag_hotness": tag_hotness,
            "domain_penalty": domain_penalty,
        }


class TemporalRanking(PostRanking):
    def story_hotness(self, story: "sic.models.Story") -> int:
        return story.created.timestamp()
