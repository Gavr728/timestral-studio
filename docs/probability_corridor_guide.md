# Коридор вероятности / Probability range

[Русский](#русский) · [English](#english)

---

## Русский

### Коридор вероятности: когда какой выбирать

Коридор показывает диапазон, в который, по мнению модели, попадёт фактическое значение. Модель выдаёт не одну цифру, а распределение возможных исходов. Линия «Прогноз» — его середина (медиана). Коридор 10–90% отсекает 10% самых низких и 10% самых высоких исходов, значит, модель ожидает, что факт попадёт внутрь примерно в 8 случаях из 10. Это работает, если модель откалибрована, а это не гарантия (см. ограничения ниже).

Выбор коридора не меняет линию прогноза, только ширину диапазона вокруг неё. Чем шире коридор, тем реже факт выходит за границы, но тем меньше пользы от самого диапазона.

| Коридор | Факт внутри | Когда брать |
|---|---|---|
| 25–75% (узкий) | ~50% случаев | Показать «типичный» сценарий. Мимо коридора факт будет выходить в половине случаев, так что как границу риска использовать нельзя. |
| 10–90% (стандарт) | ~80% | Обычное планирование и отчёты. Хороший компромисс. |
| 5–95% (широкий) | ~90% | Когда ошибка дорого стоит: страховой запас, бюджет с запасом, загрузка мощностей. |
| 1–99% (макс.) | ~98% | Стресс-тест «что в худшем случае». Диапазон часто получается слишком широким, чтобы по нему решать, а крайние квантили, как правило, оцениваются наименее надёжно. |

Проценты в таблице — теоретические значения для откалиброванной модели, а не измеренная точность на ваших данных.

### Границы можно читать по отдельности

Верхняя граница коридора 5–95% означает «в 95% случаев будет не больше». Для склада так и работает: нужный уровень сервиса соответствует верхней границе. Для рисков нехватки денег или выручки смотрите на нижнюю границу.

### Ограничения

- **Калибровка.** Заявленные 80% или 90% модель не гарантирует. Проверка на истории сейчас считает ошибку медианы и не проверяет, как часто факт попадал в коридор. Поэтому пока верить процентам стоит с оговоркой.
- **Сумма по категориям.** Таблица суммы показывает сумму границ, а она шире настоящего интервала для суммы. Для неё коридор получается консервативным.
- **Длинный горизонт.** Чем дальше шаг, тем шире коридор, это нормально.

### Пока не реализовано

Показатель покрытия в проверке на истории («коридор 80% накрыл 62% фактов») и график с попаданиями. Они позволили бы подтвердить выбор коридора цифрами.

---

## English

### Probability range: which one to choose and when

The range shows the interval in which, according to the model, the actual value will fall. The model produces not a single number but a distribution of possible outcomes. The "Forecast" line is its middle (the median). The 10–90% range cuts off the 10% lowest and the 10% highest outcomes, so the model expects the actual value to fall inside in about 8 cases out of 10. This holds if the model is calibrated, which is not guaranteed (see the limitations below).

Choosing a range does not change the forecast line, only the width of the interval around it. The wider the range, the less often the actual value leaves the bounds, but the less useful the interval itself is.

| Range | Actual inside | When to use |
|---|---|---|
| 25–75% (narrow) | ~50% of cases | Show the "typical" scenario. The actual value will fall outside the range in half of the cases, so it cannot be used as a risk boundary. |
| 10–90% (standard) | ~80% | Regular planning and reports. A good compromise. |
| 5–95% (wide) | ~90% | When an error is expensive: safety stock, a budget with a margin, capacity loading. |
| 1–99% (max) | ~98% | A stress test for "what is the worst case". The range is often too wide to base decisions on, and extreme quantiles are, as a rule, the least reliably estimated. |

The percentages in the table are theoretical values for a calibrated model, not measured accuracy on your data.

### The bounds can be read separately

The upper bound of the 5–95% range means "in 95% of cases it will be no more than this". That is how it works for a warehouse: the required service level corresponds to the upper bound. For the risk of a cash or revenue shortfall, look at the lower bound.

### Limitations

- **Calibration.** The model does not guarantee the stated 80% or 90%. The history check currently computes the error of the median and does not check how often the actual value fell inside the range. So for now, treat the percentages with caution.
- **Sum across categories.** The sum table shows the sum of the bounds, which is wider than the true interval for the sum. The range for it is conservative.
- **Long horizon.** The further the step, the wider the range; this is normal.

### Not implemented yet

A coverage metric in the history check ("the 80% range covered 62% of the actual values") and a chart of hits. They would let you back the choice of range with numbers.
